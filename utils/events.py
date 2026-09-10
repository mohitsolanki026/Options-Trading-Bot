"""
The event bus — one place every notable thing the bot does flows through.

Before this module, an alert existed only as a Telegram message, so the record
of most decisions lived in a chat history and nowhere the software could read.
Now a single ``emit`` call fans out to three places:

  1. the SQLite ``events`` table, which is the durable record,
  2. an in-memory ring buffer, which the dashboard reads instantly, and
  3. Telegram, on a background worker so a slow network call can never stall
     the one-second tick loop.

Subscribers (the dashboard's live stream) get their own queue and are dropped
cleanly if they stop draining it.
"""

import logging
import queue
import threading
from collections import deque

from utils import settings_store
from utils.trade_journal import log_event

logger = logging.getLogger(__name__)

# level -> (telegram emoji, whether it is worth pushing to the phone by default)
LEVELS = {
    "info":    ("🔵", False),
    "success": ("🟢", True),
    "warning": ("⚠️", True),
    "error":   ("❌", True),
}

RING_SIZE = 250
_recent = deque(maxlen=RING_SIZE)
_lock = threading.RLock()
_subscribers = []            # list of queue.Queue
_tg_queue = queue.Queue(maxsize=200)
_tg_worker = None


# ─────────────────────────────────────────
#  TELEGRAM WORKER
# ─────────────────────────────────────────

def _telegram_loop():
    from utils.telegram_helper import send_message
    while True:
        text = _tg_queue.get()
        try:
            send_message(text)
        except Exception as e:                      # never kill the worker
            logger.error(f"❌ Telegram send failed: {e}")
        finally:
            _tg_queue.task_done()


def _ensure_worker():
    global _tg_worker
    if _tg_worker is None or not _tg_worker.is_alive():
        _tg_worker = threading.Thread(target=_telegram_loop, daemon=True,
                                      name="TelegramSender")
        _tg_worker.start()


def _format_for_telegram(event: dict) -> str:
    emoji = LEVELS.get(event["level"], LEVELS["info"])[0]
    where = f" [{event['index']}]" if event.get("index") else ""
    lines = [f"{emoji} <b>{event['title']}</b>{where}"]
    if event.get("body"):
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(event["body"])
    return "\n".join(lines)


# ─────────────────────────────────────────
#  EMIT
# ─────────────────────────────────────────

def emit(kind: str, title: str, body: str = "", index: str = None,
         level: str = "info", meta: dict = None, telegram: bool = None) -> dict:
    """
    Record one event everywhere at once.

    kind      : machine-readable category, e.g. "trade.enter", "gate.blocked".
    telegram  : force push on/off. Left None it follows the level's default and
                the user's "Send Telegram alerts" setting.
    """
    if level not in LEVELS:
        level = "info"

    try:
        event_id = log_event(level, kind, title, body, index_name=index, meta=meta)
    except Exception as e:
        # The bus must never take the bot down. Fall back to an unsaved event.
        logger.error(f"❌ Could not persist event '{title}': {e}")
        event_id = -1

    from datetime import datetime
    now = datetime.now()
    event = {
        "id": event_id, "kind": kind, "level": level, "title": title,
        "body": body, "index": index, "meta": meta,
        "ts": now.isoformat(timespec="seconds"),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
    }

    with _lock:
        _recent.append(event)
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)          # a subscriber that stopped reading
        for q in dead:
            _subscribers.remove(q)

    push = LEVELS[level][1] if telegram is None else telegram
    if push and settings_store.get("telegram_enabled"):
        _ensure_worker()
        try:
            _tg_queue.put_nowait(_format_for_telegram(event))
        except queue.Full:
            logger.warning("⚠️ Telegram backlog full — dropping one alert.")

    log_line = f"{title}" + (f" [{index}]" if index else "")
    getattr(logger, "error" if level == "error" else
                    "warning" if level == "warning" else "info")(f"📣 {log_line}")
    return event


# convenience wrappers, so call sites read as prose
def info(kind, title, body="", index=None, **kw):
    return emit(kind, title, body, index, "info", **kw)


def success(kind, title, body="", index=None, **kw):
    return emit(kind, title, body, index, "success", **kw)


def warning(kind, title, body="", index=None, **kw):
    return emit(kind, title, body, index, "warning", **kw)


def error(kind, title, body="", index=None, **kw):
    return emit(kind, title, body, index, "error", **kw)


# ─────────────────────────────────────────
#  SUBSCRIBE  (dashboard live stream)
# ─────────────────────────────────────────

def subscribe(maxsize: int = 100) -> queue.Queue:
    q = queue.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue):
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)


def prime(rows: list):
    """
    Seed the ring buffer from the journal at startup, so the dashboard shows
    today's history immediately after a restart instead of an empty timeline.
    """
    with _lock:
        _recent.clear()
        for row in reversed(rows or []):        # journal returns newest first
            _recent.append(row)


def recent(limit: int = 50) -> list:
    """Newest first, straight from memory — no database round trip."""
    with _lock:
        return list(_recent)[-limit:][::-1]


def _reset_for_tests():
    with _lock:
        _recent.clear()
        _subscribers.clear()
