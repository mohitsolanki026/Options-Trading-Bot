"""
Event / expiry blackout for new entries.

Selling premium into a scheduled macro event (RBI/Fed/Budget) or into expiry-day
gamma is how a theta book blows up. This blocks NEW entries on:
  * configured event dates (env EVENT_BLACKOUT_DATES or data/event_blackout.json), and
  * expiry day (days_to_expiry <= EXPIRY_BLACKOUT_DTE).

Live macro-calendar fetching is intentionally out of scope (unreliable to do
headless); maintain the date list manually — it's the high-signal subset anyway.
"""

import json
import logging
import os
from datetime import date

from utils import settings_store

logger = logging.getLogger(__name__)

EVENT_FILE = "data/event_blackout.json"
EXPIRY_BLACKOUT_DTE = int(os.getenv("EXPIRY_BLACKOUT_DTE", 0))   # fallback only


def _event_dates() -> set:
    """
    Union of the dates set in the dashboard, the env list, and
    data/event_blackout.json. Any one of them can block a day.
    """
    dates = set()
    try:
        dates.update(settings_store.get("event_blackout_dates") or [])
    except Exception:
        pass
    env = os.getenv("EVENT_BLACKOUT_DATES", "")
    dates.update(d.strip() for d in env.split(",") if d.strip())
    if os.path.exists(EVENT_FILE):
        try:
            with open(EVENT_FILE) as f:
                payload = json.load(f)
            dates.update(payload if isinstance(payload, list) else payload.get("dates", []))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"❌ event_blackout.json read failed: {e}")
    return dates


def is_blackout(days_to_expiry, today: str = None) -> tuple[bool, str]:
    """Return (blocked, reason) for new entries today."""
    today = today or date.today().isoformat()
    if today in _event_dates():
        return True, f"scheduled event ({today})"
    try:
        chosen = settings_store.override("expiry_blackout_dte")
    except Exception:
        chosen = None
    dte_limit = EXPIRY_BLACKOUT_DTE if chosen is None else chosen
    if days_to_expiry is not None and days_to_expiry <= dte_limit:
        return True, f"expiry day (DTE {days_to_expiry})"
    return False, ""
