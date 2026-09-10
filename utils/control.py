"""
The control queue — how the dashboard asks the bot to do something.

The web server runs in its own thread and must never mutate a position
directly; a half-applied exit racing the tick monitor would corrupt the paper
account. Instead the web layer submits a command here, and the tick monitor,
which already wakes every second and holds the shared state, drains and runs it.

Commands that are only a flag (pause, resume) are applied straight away by the
risk manager, because they touch one boolean and need to feel instant. Anything
that opens or closes a position goes through this queue.
"""

import itertools
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

ACTIONS = ("close_position", "close_all", "rescan")

_lock = threading.RLock()
_queue = []
_results = {}
_ids = itertools.count(1)
MAX_RESULTS = 50


class UnknownAction(ValueError):
    """The submitted action is not one this bot knows how to run."""


def submit(action: str, **params) -> dict:
    """Queue a command. Returns a receipt whose id can be polled for a result."""
    if action not in ACTIONS:
        raise UnknownAction(f"Unknown action '{action}'")
    cmd = {
        "id":     next(_ids),
        "action": action,
        "params": params,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        _queue.append(cmd)
    logger.info(f"🎛️ Command queued: {action} {params or ''}")
    return {"id": cmd["id"], "action": action, "status": "queued"}


def drain() -> list:
    """Take everything queued. Called by the tick monitor once per second."""
    with _lock:
        if not _queue:
            return []
        taken, _queue[:] = list(_queue), []
    return taken


def complete(cmd: dict, ok: bool, message: str = ""):
    """Record the outcome so the dashboard can confirm what happened."""
    with _lock:
        _results[cmd["id"]] = {
            "id": cmd["id"], "action": cmd["action"], "ok": ok,
            "message": message,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        if len(_results) > MAX_RESULTS:
            for key in sorted(_results)[:len(_results) - MAX_RESULTS]:
                _results.pop(key, None)


def result(cmd_id: int):
    with _lock:
        return _results.get(cmd_id)


def pending() -> int:
    with _lock:
        return len(_queue)


def _reset_for_tests():
    with _lock:
        _queue.clear()
        _results.clear()
