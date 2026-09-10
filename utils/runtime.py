"""
Live runtime state that only exists while the bot is running.

Two things live here that were previously unobservable:

  * **Health** — when the broker login happened, whether the price feed is
    connected, how stale the last tick is, when the last scan ran, the last
    error. All of it used to be inferable only by reading log files, which is
    no use to someone asking "is it broken, or is it just quiet?".

  * **Intraday series** — the tick monitor computes an open position's profit
    every second and throws it away. The dashboard's charts need the day's
    shape, so those readings are sampled into small ring buffers here. They are
    deliberately in memory only: they are worth nothing tomorrow, and the
    durable record is the journal.

Both are safe to read from the web thread while the bot writes them.
"""

import threading
from datetime import date, datetime

MAX_POINTS = 2000      # ~8 hours at one point every 15 seconds
DEFAULT_GAP = 15.0     # seconds between stored points


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


class Health:
    """A flat, always-readable picture of whether each moving part is alive."""

    def __init__(self):
        self._lock = threading.RLock()
        self._d = {
            "started_at":      _now_iso(),
            "broker_ok":       False,
            "broker_login_at": None,
            "feed_connected":  False,
            "feed_last_tick":  None,
            "feed_tokens":     0,
            "last_cycle_at":   None,
            "last_cycle_ms":   None,
            "next_cycle_at":   None,
            "llm_ok":          None,
            "llm_last_at":     None,
            "telegram_last_at": None,
            "state_saved_at":  None,
            "last_error":      None,
        }

    def set(self, **kw):
        with self._lock:
            self._d.update(kw)

    def note_error(self, message: str, where: str = None):
        with self._lock:
            self._d["last_error"] = {
                "ts": _now_iso(),
                "where": where,
                "message": str(message)[:500],
            }

    def clear_error(self):
        with self._lock:
            self._d["last_error"] = None

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._d)
        d["tick_age_sec"] = _age(d.get("feed_last_tick"))
        d["cycle_age_sec"] = _age(d.get("last_cycle_at"))
        return d


def _age(iso: str):
    """Seconds since an ISO timestamp, or None if it never happened."""
    if not iso:
        return None
    try:
        return round((datetime.now() - datetime.fromisoformat(iso)).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


class Series:
    """
    Named intraday point series, sampled and reset at midnight.

    A name is ``"<kind>:<index>"``, e.g. ``"pnl:NIFTY"`` or ``"spot:BANKNIFTY"``.
    ``record`` is cheap to call every second: it stores a point only once
    ``min_gap`` seconds have passed, so the tick loop can call it freely.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._data = {}
        self._day = date.today()

    def _roll_day(self):
        today = date.today()
        if today != self._day:
            self._data.clear()
            self._day = today

    def record(self, name: str, value, ts: float = None, min_gap: float = DEFAULT_GAP):
        """Append a point if enough time has passed since the last one."""
        if value is None:
            return
        ts = ts if ts is not None else datetime.now().timestamp()
        with self._lock:
            self._roll_day()
            points = self._data.setdefault(name, [])
            if points and (ts - points[-1][0]) < min_gap:
                points[-1] = [ts, round(float(value), 2)]   # keep the latest value
                return
            points.append([ts, round(float(value), 2)])
            if len(points) > MAX_POINTS:
                del points[:len(points) - MAX_POINTS]

    def get(self, name: str) -> list:
        with self._lock:
            self._roll_day()
            return list(self._data.get(name, []))

    def names(self) -> list:
        with self._lock:
            self._roll_day()
            return sorted(self._data)

    def drop(self, name: str):
        with self._lock:
            self._data.pop(name, None)

    def clear(self):
        with self._lock:
            self._data.clear()


class Runtime:
    """Everything the dashboard needs that is not in a file or the database."""

    def __init__(self):
        self.health = Health()
        self.series = Series()
        self._state = None
        self._lock = threading.RLock()

    def bind_state(self, state: dict):
        """Called once by bot.py so the web layer can read the live STATE dict."""
        with self._lock:
            self._state = state

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def ready(self) -> bool:
        return self._state is not None


RUNTIME = Runtime()
