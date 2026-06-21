"""
Per-index implied-volatility history → IV Rank/Percentile.

Selling option premium only has edge when IV is rich *relative to its own recent
history*. Absolute IV (e.g. "IV > 20") is a poor proxy. This module records one
ATM IV reading per index per day and computes an IV rank (0-100) over a trailing
window, persisted to disk so it survives restarts.

Until enough history is accumulated, get_iv_rank returns None ("unknown") and the
signal engine treats premium-selling as not-yet-permitted — fail safe.
"""

import json
import os
import logging
import threading
from datetime import date

from utils.greeks_engine import calculate_iv_rank

logger = logging.getLogger(__name__)

IV_HISTORY_FILE = "data/iv_history.json"
MAX_DAYS    = 252            # ~1 trading year
MIN_HISTORY = int(os.getenv("IV_MIN_HISTORY", 10))   # need this many days first

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(IV_HISTORY_FILE):
        return {}
    try:
        with open(IV_HISTORY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"❌ IV history load failed: {e}")
        return {}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    tmp = IV_HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, IV_HISTORY_FILE)


def record_iv(index: str, avg_iv: float, today: str = None) -> None:
    """Record one ATM IV reading per index per day (last write of the day wins)."""
    if not avg_iv or avg_iv <= 0:
        return
    today = today or date.today().isoformat()
    with _lock:
        data = _load()
        series = data.get(index, [])
        if series and series[-1].get("d") == today:
            series[-1]["iv"] = round(float(avg_iv), 2)   # update today's reading
        else:
            series.append({"d": today, "iv": round(float(avg_iv), 2)})
        data[index] = series[-MAX_DAYS:]
        _save(data)


def get_iv_rank(index: str, current_iv: float):
    """
    IV rank (0-100) of current_iv vs this index's trailing history.
    Returns None until MIN_HISTORY readings exist (treated as 'unknown').
    """
    with _lock:
        data = _load()
    series = data.get(index, [])
    ivs = [row["iv"] for row in series if row.get("iv")]
    if len(ivs) < MIN_HISTORY:
        logger.info(f"📈 {index} IV history {len(ivs)}/{MIN_HISTORY} — IV rank unknown.")
        return None
    return calculate_iv_rank(current_iv, ivs)
