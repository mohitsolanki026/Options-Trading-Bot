"""
The dashboard's HTTP surface.

Reads are plain JSON built by ``serializers``. Writes are deliberately narrow:
pausing is applied straight away because it is one flag and needs to feel
instant, while anything that opens or closes a position is handed to the
control queue and executed by the tick loop, which already owns the state.
"""

import asyncio
import json
import logging
import queue
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from utils import control, events, settings_store
from utils.runtime import RUNTIME
from utils.trade_journal import get_events
from web import serializers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

STREAM_STATE_EVERY = 5.0     # seconds between unprompted state pushes


# ─────────────────────────────────────────
#  READS
# ─────────────────────────────────────────

@router.get("/state")
async def state():
    return await run_in_threadpool(serializers.full_state)


@router.get("/trades")
async def trades(limit: int = 60):
    return await run_in_threadpool(serializers.trades_view, min(max(limit, 1), 500))


@router.get("/health")
async def health():
    return await run_in_threadpool(lambda: serializers.health_view(RUNTIME.state))


@router.get("/events")
async def event_log(limit: int = 80, since: int = None):
    return await run_in_threadpool(
        lambda: {"events": get_events(limit=min(max(limit, 1), 500), since_id=since)})


@router.get("/settings")
async def read_settings():
    return {"groups": settings_store.GROUP_LABELS,
            "settings": settings_store.describe()}


# ─────────────────────────────────────────
#  LIVE STREAM
# ─────────────────────────────────────────

def _sse(name: str, payload) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.get("/stream")
async def stream(request: Request):
    """
    Server-sent events: every new activity item as it happens, plus a full state
    push on change and at least every few seconds so the clock and prices stay
    honest even on a quiet day.
    """
    subscription = events.subscribe()

    async def generate():
        try:
            yield _sse("state", await run_in_threadpool(serializers.full_state))
            last_push = time.time()
            while True:
                if await request.is_disconnected():
                    break
                drained = []
                while True:
                    try:
                        drained.append(subscription.get_nowait())
                    except queue.Empty:
                        break
                for item in drained:
                    yield _sse("activity", item)

                now = time.time()
                if drained or (now - last_push) >= STREAM_STATE_EVERY:
                    last_push = now
                    yield _sse("state", await run_in_threadpool(serializers.full_state))
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Dashboard stream ended: {e}")
        finally:
            events.unsubscribe(subscription)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",      # so nginx does not buffer the stream
    })


# ─────────────────────────────────────────
#  WRITES
# ─────────────────────────────────────────

@router.post("/settings")
async def write_settings(changes: dict = Body(...)):
    applied, errors = await run_in_threadpool(settings_store.update, changes)
    if applied:
        events.info("settings.changed", "Settings updated",
                    "Changed: " + ", ".join(
                        settings_store.BY_KEY[k].label for k in sorted(applied)),
                    telegram=False)
        rm = (RUNTIME.state or {}).get("risk_manager")
        if rm:
            rm.refresh()
    return {"applied": applied, "errors": errors,
            "settings": await run_in_threadpool(settings_store.describe)}


@router.post("/settings/reset")
async def reset_settings():
    await run_in_threadpool(settings_store.reset)
    rm = (RUNTIME.state or {}).get("risk_manager")
    if rm:
        rm.refresh()
    events.info("settings.reset", "Settings reset to defaults", telegram=False)
    return {"settings": await run_in_threadpool(settings_store.describe)}


def _risk_manager():
    rm = (RUNTIME.state or {}).get("risk_manager")
    if rm is None:
        raise HTTPException(status_code=503,
                            detail="The bot is still starting up. Try again in a moment.")
    return rm


@router.post("/control/pause")
async def pause():
    rm = _risk_manager()
    changed = rm.pause("Paused from the dashboard", manual=True)
    if changed:
        events.warning("control.pause", "New trades paused",
                       "Open positions are still watched and will still hit "
                       "their stop loss or target.")
    return {"ok": True, "paused": True, "changed": changed,
            "message": "New trades are paused."}


@router.post("/control/resume")
async def resume():
    rm = _risk_manager()
    ok, message = rm.resume()
    if ok:
        events.success("control.resume", "Trading resumed",
                       "The bot will look for trades again at the next check.")
    return {"ok": ok, "paused": not ok, "message": message}


@router.post("/control/close")
async def close_position(payload: dict = Body(default={})):
    index = (payload or {}).get("index")
    if not index:
        raise HTTPException(status_code=400, detail="Which index should be closed?")
    receipt = control.submit("close_position", index=index)
    return {"ok": True, **receipt,
            "message": f"Closing the {index} trade. This takes a second."}


@router.post("/control/close-all")
async def close_all():
    receipt = control.submit("close_all")
    return {"ok": True, **receipt, "message": "Closing every open trade."}


@router.post("/control/rescan")
async def rescan():
    receipt = control.submit("rescan")
    return {"ok": True, **receipt, "message": "Checking the market now."}


@router.get("/control/result/{command_id}")
async def control_result(command_id: int):
    outcome = control.result(command_id)
    if outcome is None:
        return JSONResponse({"status": "pending", "id": command_id})
    return {"status": "done", **outcome}
