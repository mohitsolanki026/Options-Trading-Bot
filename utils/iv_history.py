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
from config.settings import INDIA_VIX_TOKEN

logger = logging.getLogger(__name__)

IV_HISTORY_FILE = "data/iv_history.json"
MAX_DAYS    = 252            # ~1 trading year
MIN_HISTORY = int(os.getenv("IV_MIN_HISTORY", 10))   # need this many days first

_lock = threading.Lock()
_vix_cache = {"date": None, "closes": None}   # India VIX daily closes, cached per day


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


def get_vix_rank(obj, current_vix: float):
    """
    India-VIX percentile over its trailing daily history — a market-wide IV-rank
    PROXY usable on day 1 (VIX has years of history), used until per-index IV
    history accumulates. VIX is NIFTY's implied vol; for BANKNIFTY it is a
    correlated approximation. Daily closes are cached once per day.
    """
    if not obj or not current_vix:
        return None
    today = date.today().isoformat()
    if _vix_cache["date"] != today or _vix_cache["closes"] is None:
        try:
            from utils.technical import fetch_candles
            df = fetch_candles(obj, INDIA_VIX_TOKEN, interval="ONE_DAY", days_back=150)
            _vix_cache["closes"] = list(df["close"]) if not df.empty else []
        except Exception as e:
            logger.error(f"❌ VIX history fetch failed: {e}")
            _vix_cache["closes"] = []
        _vix_cache["date"] = today
    closes = _vix_cache["closes"]
    if len(closes) < MIN_HISTORY:
        return None
    return calculate_iv_rank(current_vix, closes)


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
