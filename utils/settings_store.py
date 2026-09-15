"""
Runtime settings — file-backed overrides layered on the env defaults.

Why this module exists
----------------------
Every tunable used to be read from the environment at import time, so changing a
risk limit meant editing ``.env`` and restarting the process. The dashboard has
to change them while the bot is running, so the authoritative value now comes
from ``get(key)``: the override in ``data/settings.json`` when one exists, and
the env-derived default from ``config/settings.py`` otherwise.

Rules
  * The env value is the DEFAULT. Once an override exists it wins.
  * Every write is validated against ``SPEC`` and persisted atomically.
  * ``version()`` increments on every change, so long-lived objects (the risk
    manager) can cheaply notice they are stale and refresh.
  * Unknown keys are rejected rather than silently stored.
"""

import json
import logging
import os
import re
import threading

from config import settings as env

logger = logging.getLogger(__name__)

SETTINGS_FILE = "data/settings.json"

_lock = threading.RLock()
_overrides = None      # lazily loaded dict
_version = 0

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Setting:
    """One tunable: how to show it, how to validate it, what it defaults to."""

    __slots__ = ("key", "group", "label", "help", "kind", "default",
                 "min", "max", "choices", "editable")

    def __init__(self, key, group, label, help, kind, default,
                 min=None, max=None, choices=None, editable=True):
        self.key, self.group, self.label, self.help = key, group, label, help
        self.kind, self.default = kind, default
        self.min, self.max, self.choices, self.editable = min, max, choices, editable


def _indices_default():
    return list(env.ACTIVE_INDICES)


def _event_dates_default():
    raw = os.getenv("EVENT_BLACKOUT_DATES", "")
    return [d.strip() for d in raw.split(",") if d.strip()]


# ─────────────────────────────────────────
#  THE TUNABLES
# ─────────────────────────────────────────
SPEC = [
    # ── money ──
    Setting("paper_capital", "money", "Starting capital",
            "The virtual money the bot began with. Changing this only takes effect "
            "when you reset the paper account.",
            "money", env.PAPER_CAPITAL, min=10000, editable=False),
    Setting("max_capital_per_trade", "money", "Most to commit per trade",
            "Share of the account a single trade may tie up as broker margin.",
            "pct", env.RISK_RULES["max_capital_per_trade"], min=0.01, max=1.0),
    Setting("max_daily_loss", "money", "Stop for the day after losing",
            "Once today's realised loss reaches this, the bot halts until tomorrow.",
            "loss", env.RISK_RULES["max_daily_loss"], min=-10_000_000, max=-100),
    Setting("max_per_trade_loss", "money", "Most to lose on one trade",
            "Used when sizing a position, before real broker margin is applied.",
            "loss", env.RISK_RULES["max_per_trade_loss"], min=-10_000_000, max=-100),
    Setting("max_open_positions", "money", "Most trades open at once",
            "Counted across every index together.",
            "int", env.RISK_RULES["max_open_positions"], min=1, max=10),
    Setting("max_lots_per_trade", "money", "Most lots in one trade",
            "A hard cap on top of the risk-based sizing. Kept small until the "
            "paper results earn a bigger one.",
            "int", int(os.getenv("MAX_LOTS_PER_TRADE", 2)), min=1, max=50),
    Setting("paper_slippage_pct", "money", "Assumed slippage per leg",
            "Paper fills are worsened by this share of the option price on the "
            "way in and out, to stand in for the bid-ask spread.",
            "float", float(os.getenv("PAPER_SLIPPAGE_PCT", 0.005)), min=0.0, max=0.05),
    Setting("paper_charges_per_order", "money", "Charges per order leg",
            "Brokerage, STT and exchange fees, deducted for every leg on entry "
            "and on exit. ₹25 is a fair estimate for Angel One index options.",
            "money", float(os.getenv("PAPER_CHARGES_PER_ORDER", 25)), min=0, max=500),

    # ── scope ──
    Setting("active_indices", "scope", "Indices to watch",
            "Each one is scanned independently and can hold its own position.",
            "indices", _indices_default(), choices=list(env.INDICES.keys())),

    # ── timing ──
    Setting("hold_mode", "timing", "How long trades are held",
            "Positional holds a trade until its target, its stop, or the morning "
            "of expiry. Intraday closes everything before the market shuts, which "
            "gives sold premium almost no time to decay.",
            "choice", os.getenv("HOLD_MODE", "positional").strip().lower(),
            choices=["positional", "intraday"]),
    Setting("max_hold_days", "timing", "Most days to hold a trade",
            "Positional mode only. The trade is closed at the first check on the "
            "day this is reached, even if it is neither at target nor at stop.",
            "int", int(os.getenv("MAX_HOLD_DAYS", 5)), min=1, max=30),
    Setting("entry_window_start", "timing", "Open new trades from",
            "Avoids the noisy opening minutes.",
            "time", os.getenv("ENTRY_WINDOW_START", "09:40")),
    Setting("entry_window_end", "timing", "Open new trades until",
            "Nothing new opens after this, so every trade has room to work.",
            "time", os.getenv("ENTRY_WINDOW_END", "14:00")),
    Setting("entry_cooldown_min", "timing", "Wait after closing a trade",
            "Minutes before the same index may be re-entered.",
            "int", env.ENTRY_COOLDOWN_MIN, min=0, max=240),
    Setting("expiry_blackout_dte", "timing", "Skip expiry days",
            "Block new entries when days-to-expiry is at or below this. The last "
            "two days are where a small move swings option prices the most.",
            "int", int(os.getenv("EXPIRY_BLACKOUT_DTE", 2)), min=0, max=10),
    Setting("event_blackout_dates", "timing", "Skip these event dates",
            "Budget day, RBI policy, US Fed decisions. One ISO date per entry.",
            "dates", _event_dates_default()),

    # ── strictness ──
    Setting("entry_threshold", "strictness", "Signal points needed to trade",
            "Out of 10. Higher means fewer but stronger trades.",
            "int", int(os.getenv("ENTRY_THRESHOLD", 4)), min=1, max=10),
    Setting("iv_rank_sell", "strictness", "Only sell options above",
            "How expensive options must be against their own recent history, out of 100.",
            "int", int(os.getenv("IV_RANK_SELL", 55)), min=0, max=100),
    Setting("iv_rank_buy", "strictness", "Only buy options below",
            "Below this, options are cheap enough that buying has an edge.",
            "int", int(os.getenv("IV_RANK_BUY", 30)), min=0, max=100),
    Setting("max_correlated_short", "strictness", "Correlated short trades allowed",
            "NIFTY, BANKNIFTY and FINNIFTY move together. This caps how many "
            "sold-premium trades may run across that group at once.",
            "int", env.MAX_CORRELATED_SHORT, min=1, max=3),
    Setting("min_leg_oi", "strictness", "Skip options thinner than",
            "Minimum open interest on a leg, so the bot avoids illiquid strikes.",
            "int", env.MIN_LEG_OI, min=0, max=100000),
    Setting("min_credit_pct", "strictness", "Minimum premium to bother selling",
            "Net credit as a share of the index price. Stops the bot selling "
            "near-worthless premium for full tail risk.",
            "float", env.MIN_CREDIT_PCT, min=0.0, max=0.05),
    Setting("strangle_target_delta", "strictness", "Strangle strike delta",
            "How far out of the money strangle legs sit. Lower is further away "
            "and safer, with less premium.",
            "float", env.STRANGLE_TARGET_DELTA, min=0.05, max=0.45),
    Setting("short_premium_stop_pct", "strictness", "Stop on sold premium",
            "Close a sold-options trade once the premium has risen by this share "
            "of what was collected. 1.0 means the premium has doubled.",
            "float", float(os.getenv("SHORT_PREMIUM_STOP_PCT", 1.0)), min=0.2, max=3.0),
    Setting("short_premium_target_pct", "strictness", "Target on sold premium",
            "Close a sold-options trade once this share of the premium has decayed.",
            "float", float(os.getenv("SHORT_PREMIUM_TARGET_PCT", 0.5)), min=0.1, max=0.9),
    Setting("iv_min_history", "strictness", "Days of history before selling",
            "An index's own IV rank is trusted only once this many daily readings "
            "exist. Until then the India VIX percentile stands in for it.",
            "int", int(os.getenv("IV_MIN_HISTORY", 60)), min=5, max=252),

    # ── alerts ──
    Setting("telegram_enabled", "alerts", "Send Telegram alerts",
            "Turn off to keep everything in the dashboard only.",
            "bool", os.getenv("TELEGRAM_ENABLED", "1") not in ("0", "false", "False")),
]

BY_KEY = {s.key: s for s in SPEC}

GROUP_LABELS = {
    "money":      "Money",
    "scope":      "What to watch",
    "timing":     "When to trade",
    "strictness": "How picky to be",
    "alerts":     "Alerts",
}


# ─────────────────────────────────────────
#  VALIDATION
# ─────────────────────────────────────────

class SettingError(ValueError):
    """A submitted value is not usable for this setting."""


def coerce(spec: Setting, value):
    """Convert a submitted value to its stored type, or raise SettingError."""
    k = spec.kind
    try:
        if k == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        if k == "choice":
            out = str(value).strip().lower()
            if out not in (spec.choices or []):
                raise SettingError(f"{spec.label}: choose one of "
                                   f"{', '.join(spec.choices or [])}.")
            return out

        if k == "int":
            out = int(round(float(value)))
        elif k in ("float", "pct", "money", "loss"):
            out = float(value)
        elif k == "time":
            out = str(value).strip()
            if not _TIME_RE.match(out):
                raise SettingError(f"{spec.label}: use a 24-hour time like 09:40.")
            return out
        elif k == "indices":
            if isinstance(value, str):
                value = [p for p in value.replace(",", " ").split() if p]
            out = [str(v).strip().upper() for v in value]
            bad = [v for v in out if v not in (spec.choices or [])]
            if bad:
                raise SettingError(f"{spec.label}: unknown index {', '.join(bad)}.")
            if not out:
                raise SettingError(f"{spec.label}: pick at least one index.")
            return sorted(set(out), key=out.index)
        elif k == "dates":
            if isinstance(value, str):
                value = [p for p in value.replace(",", " ").split() if p]
            out = [str(v).strip() for v in value]
            bad = [v for v in out if not _DATE_RE.match(v)]
            if bad:
                raise SettingError(f"{spec.label}: use YYYY-MM-DD ({', '.join(bad)}).")
            return sorted(set(out))
        else:
            raise SettingError(f"{spec.label}: unsupported type {k}.")
    except SettingError:
        raise
    except (TypeError, ValueError):
        raise SettingError(f"{spec.label}: '{value}' is not a number.")

    if spec.min is not None and out < spec.min:
        raise SettingError(f"{spec.label}: cannot be below {spec.min}.")
    if spec.max is not None and out > spec.max:
        raise SettingError(f"{spec.label}: cannot be above {spec.max}.")
    return out


# ─────────────────────────────────────────
#  STORAGE
# ─────────────────────────────────────────

def _load() -> dict:
    global _overrides
    if _overrides is not None:
        return _overrides
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                raw = json.load(f)
            for key, val in (raw or {}).items():
                spec = BY_KEY.get(key)
                if not spec:
                    logger.warning(f"⚠️ Dropping unknown saved setting '{key}'.")
                    continue
                try:
                    data[key] = coerce(spec, val)
                except SettingError as e:
                    logger.warning(f"⚠️ Dropping invalid saved setting: {e}")
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"❌ Could not read {SETTINGS_FILE}: {e} — using defaults.")
    _overrides = data
    return _overrides


def _persist():
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_overrides, f, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_FILE)


# ─────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────

def get(key: str):
    """Current value: the saved override if there is one, else the env default."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"Unknown setting '{key}'")
    with _lock:
        return _load().get(key, spec.default)


def override(key: str):
    """
    The saved override for a key, or None if the user has never changed it.

    Modules use this so an explicit dashboard change always wins, while an
    untouched setting still resolves through the module's own env-derived
    constant. That keeps one source of truth without breaking env config.
    """
    if key not in BY_KEY:
        raise KeyError(f"Unknown setting '{key}'")
    with _lock:
        return _load().get(key)


def all_values() -> dict:
    with _lock:
        over = _load()
        return {s.key: over.get(s.key, s.default) for s in SPEC}


def version() -> int:
    """Bumped on every successful change. Cheap staleness check."""
    return _version


def update(changes: dict) -> tuple[dict, list[str]]:
    """
    Apply a batch of changes. Returns ``(applied, errors)``.

    Valid keys are applied together; invalid ones are reported and nothing about
    them is stored. A key set back to its default is removed from the override
    file rather than pinned, so future env changes still reach it.
    """
    global _version
    applied, errors = {}, []
    with _lock:
        over = _load()
        staged = {}
        for key, raw in (changes or {}).items():
            spec = BY_KEY.get(key)
            if spec is None:
                errors.append(f"Unknown setting '{key}'.")
                continue
            if not spec.editable:
                errors.append(f"{spec.label} cannot be changed from here.")
                continue
            try:
                staged[key] = coerce(spec, raw)
            except SettingError as e:
                errors.append(str(e))

        # Cross-field sanity: a buy threshold above the sell threshold is
        # contradictory and would make both premium rules unreachable.
        sell = staged.get("iv_rank_sell", over.get("iv_rank_sell", BY_KEY["iv_rank_sell"].default))
        buy  = staged.get("iv_rank_buy",  over.get("iv_rank_buy",  BY_KEY["iv_rank_buy"].default))
        if buy >= sell:
            errors.append("'Only buy options below' must be lower than "
                          "'Only sell options above'.")
            staged.pop("iv_rank_sell", None)
            staged.pop("iv_rank_buy", None)

        start = staged.get("entry_window_start",
                           over.get("entry_window_start", BY_KEY["entry_window_start"].default))
        end = staged.get("entry_window_end",
                         over.get("entry_window_end", BY_KEY["entry_window_end"].default))
        if start >= end:
            errors.append("The entry window must start before it ends.")
            staged.pop("entry_window_start", None)
            staged.pop("entry_window_end", None)

        for key, val in staged.items():
            if val == BY_KEY[key].default:
                over.pop(key, None)
            else:
                over[key] = val
            applied[key] = val

        if applied:
            _persist()
            _version += 1
            logger.info(f"⚙️ Settings updated: {', '.join(sorted(applied))}")

    return applied, errors


def reset() -> None:
    """Drop every override and go back to the env defaults."""
    global _overrides, _version
    with _lock:
        _overrides = {}
        _persist()
        _version += 1
    logger.info("⚙️ Settings reset to defaults.")


def describe() -> list[dict]:
    """Everything the dashboard needs to render the settings screen."""
    with _lock:
        over = _load()
        return [{
            "key":       s.key,
            "group":     s.group,
            "groupLabel": GROUP_LABELS.get(s.group, s.group),
            "label":     s.label,
            "help":      s.help,
            "kind":      s.kind,
            "value":     over.get(s.key, s.default),
            "default":   s.default,
            "min":       s.min,
            "max":       s.max,
            "choices":   s.choices,
            "editable":  s.editable,
            "overridden": s.key in over,
        } for s in SPEC]


def _reset_cache_for_tests(path: str = None):
    """Point the store at a fresh file and drop the cache. Tests only."""
    global _overrides, _version, SETTINGS_FILE
    with _lock:
        if path:
            SETTINGS_FILE = path
        _overrides = None
        _version = 0
