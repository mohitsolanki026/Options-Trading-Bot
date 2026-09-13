"""
Turning live bot state into JSON the dashboard can draw.

Everything the API returns is built here, for two reasons. The live state holds
pandas DataFrames and other objects that must never reach a JSON encoder, and
the dashboard is written for someone who does not trade professionally, so the
plain-English wording belongs in one reviewable place rather than scattered
through templates.
"""

import time
from datetime import datetime, timedelta

from config.settings import INDICES, PAPER_CAPITAL
from utils import events, iv_history, settings_store, strategies
from utils.runtime import RUNTIME
from utils.trade_journal import (
    get_closed_trades, get_daily_pnl, get_equity_series, get_latest_gate,
)
from utils.websocket_feed import TICK_STORE

# name -> (short label, what it actually does in words)
STRATEGY_PLAIN = {
    "short_straddle":   ("Short straddle",
                         "Sold a call and a put at the same strike. Makes money if "
                         "the index barely moves."),
    "short_strangle":   ("Short strangle",
                         "Sold a call above the market and a put below it. Makes "
                         "money if the index stays in a range."),
    "long_straddle":    ("Long straddle",
                         "Bought a call and a put. Makes money on a big move in "
                         "either direction."),
    "long_ce":          ("Long call", "Bought a call. Makes money if the index rises."),
    "long_pe":          ("Long put", "Bought a put. Makes money if the index falls."),
    "bull_call_spread": ("Bull call spread",
                         "Bought a call and sold a higher one. A capped bet that "
                         "the index rises."),
    "bear_put_spread":  ("Bear put spread",
                         "Bought a put and sold a lower one. A capped bet that "
                         "the index falls."),
}

SIGNAL_PLAIN = {
    "PCR":     "Puts against calls",
    "OI":      "Where the price sits",
    "IV Rank": "Option prices vs usual",
    "VIX":     "Fear gauge",
    "Theta":   "Time decay",
    "TA":      "Chart indicators",
}

BIAS_PLAIN = {
    "SELL_PREMIUM": "Sell expensive options",
    "BUY_OPTIONS":  "Buy cheap options",
    "BULLISH":      "Leaning up",
    "BEARISH":      "Leaning down",
    "NEUTRAL":      "No clear view",
}


def to_native(obj):
    """
    Convert numpy scalars to plain Python types.

    Strikes, open interest and prices come out of pandas, so they are numpy
    types. They compare and arithmetic fine, but the JSON encoder rejects them,
    which took down the whole state endpoint rather than one field. Everything
    leaving this module goes through here.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_native(v) for v in obj]
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except (ValueError, TypeError):
            pass
    return str(obj)


def _r(value, places=2):
    """Round, tolerating None so the JSON stays honest about missing data."""
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def _tz_offset_min() -> int:
    return -int(time.timezone / 60 if not time.daylight else time.altzone / 60)


# ─────────────────────────────────────────
#  MARKET CLOCK
# ─────────────────────────────────────────

def market_view() -> dict:
    from utils.monitor import is_market_open, is_safe_to_enter, minutes_to_close
    now = datetime.now()
    weekend = now.weekday() >= 5
    t = now.hour * 60 + now.minute
    if weekend:
        phase = "weekend"
    elif t < 9 * 60 + 15:
        phase = "premarket"
    elif t <= 15 * 60 + 30:
        phase = "open"
    else:
        phase = "closed"

    close_at = now.replace(hour=15, minute=30, second=0, microsecond=0)
    to_close = max(0, int((close_at - now).total_seconds() // 60)) if phase == "open" else 0
    return {
        "phase":        phase,
        "isOpen":       phase == "open",
        "label":        now.strftime("%a %-d %b"),
        "clock":        now.strftime("%H:%M"),
        "closesInMin":  to_close,
        "tradingClose": minutes_to_close() if phase == "open" else 0,
        "entryWindowOpen": is_safe_to_enter(),
        "entryWindow":  f'{settings_store.get("entry_window_start")} – '
                        f'{settings_store.get("entry_window_end")}',
    }


def schedule_view() -> list:
    """The rest of today, in the order it will happen."""
    now = datetime.now()
    health = RUNTIME.health.snapshot()
    rows = []

    nxt = health.get("next_cycle_at")
    if nxt:
        try:
            rows.append({"time": datetime.fromisoformat(nxt).strftime("%H:%M"),
                         "label": "Next market check", "state": "next"})
        except ValueError:
            pass
    rows += [
        {"time": settings_store.get("entry_window_end"),
         "label": "Last moment a new trade can open", "state": "todo"},
        {"time": "15:00", "label": "Everything still open is closed", "state": "todo"},
        {"time": "15:30", "label": "Day's report is sent", "state": "todo"},
    ]
    for row in rows:
        try:
            hh, mm = row["time"].split(":")
            if (now.hour, now.minute) >= (int(hh), int(mm)):
                row["state"] = "done"
        except ValueError:
            pass
    return rows


# ─────────────────────────────────────────
#  ACCOUNT / RISK
# ─────────────────────────────────────────

def account_view(pt, open_pnl: float) -> dict:
    stats = pt.get_stats()
    account_value = pt.capital + (open_pnl or 0)
    start = stats["starting_capital"] or PAPER_CAPITAL or 1
    return {
        "startingCapital": _r(start),
        "settledCapital":  _r(pt.capital),
        "openPnl":         _r(open_pnl),
        "accountValue":    _r(account_value),
        "totalPnl":        _r(account_value - start),
        "returnsPct":      _r((account_value - start) / start * 100),
        "closedTrades":    stats["total_trades"],
        "wins":            stats["wins"],
        "losses":          stats["losses"],
        "winRate":         stats["win_rate"],
        "avgWin":          stats["avg_win"],
        "avgLoss":         stats["avg_loss"],
        "bestTrade":       stats["best_trade"],
        "worstTrade":      stats["worst_trade"],
        "profitFactor":    None if stats["profit_factor"] == float("inf")
                           else stats["profit_factor"],
    }


def risk_view(rm, open_count: int = None) -> dict:
    if rm is None:
        return {}
    status = rm.get_status()
    if open_count is not None:
        status["open_positions"] = open_count
    limit  = abs(status["daily_loss_limit"] or 1)
    used   = max(0.0, -float(status["daily_pnl"] or 0))
    # "Headroom" counts a winning day as extra room to lose, which is true but
    # reads as a bigger budget than the user set. The tile shows what is left
    # of the limit itself.
    left   = max(0.0, limit - used)
    return {
        "budgetLimit":      _r(limit),
        "budgetLeft":       _r(left),
        "dailyPnl":         _r(status["daily_pnl"]),
        "dailyLossLimit":   _r(status["daily_loss_limit"]),
        "budgetUsed":       _r(used),
        "budgetUsedPct":    _r(min(100.0, used / limit * 100), 1),
        "headroom":         _r(status["headroom"]),
        "openPositions":    status["open_positions"],
        "maxOpenPositions": status.get("max_open_positions"),
        "halted":           status["trading_halted"],
        "haltReason":       status["halt_reason"],
        "haltIsManual":     status.get("halt_is_manual", False),
    }


# ─────────────────────────────────────────
#  POSITIONS
# ─────────────────────────────────────────

def _price_map(position, state):
    df_oi = (state or {}).get("index_data", {}).get(position["index"], {}).get("df_oi")
    return strategies.current_price_map(position, TICK_STORE, df_oi)


def position_view(position: dict, state: dict) -> dict:
    price_map = _price_map(position, state)
    pnl       = strategies.unrealised_pnl(position, price_map)
    sl, tgt   = position["stop_loss_pnl"], position["target_pnl"]
    span      = (tgt - sl) or 1
    label, plain = STRATEGY_PLAIN.get(position["strategy"],
                                      (position["strategy"].replace("_", " ").title(), ""))

    legs = [{
        "action": leg["action"],
        "type":   leg["option_type"],
        "strike": _r(leg["strike"], 0),
        "entry":  _r(leg["entry_ltp"]),
        "now":    _r(price_map.get(leg["token"]) or leg["entry_ltp"]),
        "symbol": leg.get("symbol"),
    } for leg in position["legs"]]

    spot = (state or {}).get("index_data", {}).get(position["index"], {}).get("spot_ltp")
    if not spot:
        idx = INDICES.get(position["index"])
        spot = (TICK_STORE.get_ltp(idx["token"]) if idx else None) or None

    return {
        "index":       position["index"],
        "strategy":    position["strategy"],
        "label":       label,
        "plain":       plain,
        "direction":   position["direction"],
        "lots":        position["lots"],
        "lotSize":     position["lot_size"],
        "expiry":      position["expiry"],
        "entryTime":   position.get("entry_time"),
        "netCredit":   _r(position["net_credit"]),
        "premium":     _r(abs(position["net_credit"]) * position["lot_size"] * position["lots"]),
        "pnl":         _r(pnl),
        "stopLossPnl": _r(sl),
        "targetPnl":   _r(tgt),
        "meterPct":    _r(max(0.0, min(100.0, (pnl - sl) / span * 100)), 1),
        "zeroPct":     _r(max(0.0, min(100.0, (0 - sl) / span * 100)), 1),
        "winning":     pnl >= 0,
        "legs":        legs,
        "spot":        _r(spot),
        "range":       _range_view(position, spot),
    }


def _range_view(position: dict, spot):
    """
    The band where a sold-premium position keeps its money.

    Only produced for short positions that have both a call and a put leg —
    for anything else the idea of a "safe range" would be misleading, so the
    dashboard shows the simpler card instead.
    """
    if position["direction"] != "SELL" or not spot:
        return None
    short_ce = [l["strike"] for l in position["legs"]
                if l["option_type"] == "CE" and l["action"] == "SELL"]
    short_pe = [l["strike"] for l in position["legs"]
                if l["option_type"] == "PE" and l["action"] == "SELL"]
    if not short_ce or not short_pe:
        return None

    high, low = min(short_ce), max(short_pe)
    credit    = abs(position["net_credit"])
    be_high, be_low = high + credit, low - credit
    pad = max((be_high - be_low) * 0.12, 1)
    lo, hi = be_low - pad, be_high + pad
    width = (hi - lo) or 1

    def pct(v):
        return _r(max(0.0, min(100.0, (v - lo) / width * 100)), 2)

    return {
        "low": _r(low, 0), "high": _r(high, 0),
        "breakevenLow": _r(be_low), "breakevenHigh": _r(be_high),
        "spot": _r(spot),
        "corePct":  [pct(low), pct(high)],
        "safePct":  [pct(be_low), pct(be_high)],
        "spotPct":  pct(spot),
        "inRange":  bool(low <= spot <= high),
        "inProfit": bool(be_low <= spot <= be_high),
    }


# ─────────────────────────────────────────
#  INDICES
# ─────────────────────────────────────────

def index_view(index_key: str, state: dict, pt, active: bool) -> dict:
    data    = (state or {}).get("index_data", {}).get(index_key) or {}
    summary = data.get("summary") or {}
    greeks  = data.get("greeks") or {}
    conf    = data.get("confluence") or {}
    regime  = data.get("regime") or {}

    signals = [{
        "label":  SIGNAL_PLAIN.get(sig.get("label"), sig.get("label")),
        "value":  sig.get("value"),
        "score":  sig.get("score", 0),
        "bias":   sig.get("bias"),
    } for sig in conf.get("signals", [])]

    idx  = INDICES.get(index_key, {})
    spot = data.get("spot_ltp") or (TICK_STORE.get_ltp(idx.get("token")) if idx else None)
    spot = spot or None

    return {
        "index":       index_key,
        "active":      active,
        "hasPosition": bool(pt and pt.has_position(index_key)),
        "spot":        _r(spot),
        "expiry":      data.get("expiry"),
        "dte":         greeks.get("days_to_exp"),
        "ivRank":      _r(data.get("iv_rank"), 0),
        "pcr":         _r(summary.get("pcr")),
        "support":     _r(summary.get("support"), 0),
        "resistance":  _r(summary.get("resistance"), 0),
        "maxPain":     _r(summary.get("max_pain"), 0),
        "avgIv":       _r(greeks.get("avg_iv")),
        "theta":       _r(greeks.get("theta")),
        "regime":      regime.get("regime_label") or regime.get("regime"),
        "bias":        conf.get("overall_bias"),
        "biasPlain":   BIAS_PLAIN.get(conf.get("overall_bias"), "No clear view"),
        "score":       conf.get("score"),
        "maxScore":    conf.get("max_score"),
        "threshold":   conf.get("threshold"),
        "signals":     signals,
        "gate":        get_latest_gate(index_key),
        "lotSize":     idx.get("lot_size"),
    }


# ─────────────────────────────────────────
#  CHART SERIES
# ─────────────────────────────────────────

def series_view(indices: list, positions: list) -> dict:
    open_indices = [p["index"] for p in positions]
    return {
        "tzOffsetMin": _tz_offset_min(),
        "spot": {i: RUNTIME.series.get(f"spot:{i}") for i in indices},
        "pnl":  {i: RUNTIME.series.get(f"pnl:{i}") for i in open_indices},
        "vix":  RUNTIME.series.get("vix"),
    }


# ─────────────────────────────────────────
#  HEALTH
# ─────────────────────────────────────────

def health_view(state: dict) -> dict:
    h = RUNTIME.health.snapshot()
    pt = (state or {}).get("paper_trader")
    checks = [
        {"key": "broker", "label": "Angel One login",
         "ok": bool(h["broker_ok"]),
         "detail": (f"Signed in automatically at "
                    f"{(h['broker_login_at'] or '')[11:16]}" if h["broker_ok"]
                    else "Not signed in yet."),
         "state": "ok" if h["broker_ok"] else "bad"},
        {"key": "feed", "label": "Live prices",
         "ok": bool(h["feed_connected"]),
         "detail": (f"Last price {int(h['tick_age_sec'])}s ago"
                    if h.get("tick_age_sec") is not None
                    else "No prices received yet."),
         "state": "ok" if h["feed_connected"] and (h.get("tick_age_sec") or 999) < 120
                  else "warn" if h["feed_connected"] else "bad"},
        {"key": "cycle", "label": "Market check",
         "ok": h["last_cycle_at"] is not None,
         "detail": (f"Ran at {(h['last_cycle_at'] or '')[11:16]}, next at "
                    f"{(h['next_cycle_at'] or '')[11:16]}" if h["last_cycle_at"]
                    else "Has not run yet."),
         "state": "ok" if h["last_cycle_at"] else "warn"},
        {"key": "llm", "label": "AI second opinion",
         "ok": h["llm_ok"] is not False,
         "detail": (f"Last asked at {(h['llm_last_at'] or '')[11:16]}"
                    if h["llm_last_at"] else "Not needed yet today."),
         "state": "bad" if h["llm_ok"] is False else "ok"},
        {"key": "telegram", "label": "Telegram alerts",
         "ok": True,
         "detail": (f"Last message at {(h['telegram_last_at'] or '')[11:16]}"
                    if h["telegram_last_at"] else "Nothing sent yet today."),
         "state": "ok" if settings_store.get("telegram_enabled") else "off"},
    ]

    data_checks = []
    status = iv_history.history_status()
    for index_key in settings_store.get("active_indices"):
        row = status.get(index_key, {"days": 0, "need": 10, "ready": False})
        data_checks.append({
            "key": f"iv:{index_key}", "label": f"{index_key} history",
            "ok": row["ready"],
            "detail": (f"{row['days']} days recorded, {row['need']} needed"
                       if row["ready"] else
                       f"{row['days']} of {row['need']} days recorded. Selling "
                       f"options here stays blocked until that is met."),
            "state": "ok" if row["ready"] else "warn",
        })
    data_checks.append({
        "key": "state", "label": "Saved account state",
        "ok": bool(h["state_saved_at"]),
        "detail": (f"Written at {(h['state_saved_at'] or '')[11:16]}, survives a restart"
                   if h["state_saved_at"] else "Nothing saved yet this session."),
        "state": "ok" if h["state_saved_at"] else "warn",
    })

    all_rows = checks + data_checks
    # "off" means the user switched something off on purpose, so it is not a
    # failure. Only "warn" and "bad" mean something actually needs attention.
    return {
        "connections": checks,
        "data":        data_checks,
        "needsAttention": [r["label"] for r in all_rows if r["state"] in ("warn", "bad")],
        "passing":     sum(1 for r in all_rows if r["state"] in ("ok", "off")),
        "total":       len(all_rows),
        "lastError":   h["last_error"],
        "startedAt":   h["started_at"],
        "raw":         h,
        "positionsOpen": pt.open_count() if pt else 0,
    }


# ─────────────────────────────────────────
#  THE WHOLE PICTURE
# ─────────────────────────────────────────

def full_state() -> dict:
    state = RUNTIME.state
    now   = datetime.now()
    base  = {
        "now":     now.isoformat(timespec="seconds"),
        "mode":    "paper",
        "market":  market_view(),
        "schedule": schedule_view(),
        "health":  health_view(state),
        "activity": events.recent(40),
    }

    if not state or not state.get("paper_trader"):
        base.update({"ready": False, "positions": [], "indices": [],
                     "account": {}, "risk": {}, "series": {}, "daily": []})
        return to_native(base)

    pt = state["paper_trader"]
    rm = state.get("risk_manager")
    indices = state.get("active_indices") or settings_store.get("active_indices")

    positions = [position_view(p, state) for p in pt.open_trades.values()]
    open_pnl  = sum(p["pnl"] or 0 for p in positions)

    every = list(dict.fromkeys(list(indices) + list(INDICES)))
    base.update({
        "ready":     True,
        "daily":     get_daily_pnl(10),
        "account":   account_view(pt, open_pnl),
        "risk":      risk_view(rm, open_count=pt.open_count()),
        "positions": positions,
        "indices":   [index_view(i, state, pt, i in indices) for i in every],
        "series":    series_view(indices, positions),
    })
    return to_native(base)


def trades_view(limit: int = 60) -> dict:
    rows = get_closed_trades(limit)
    for row in rows:
        label, _ = STRATEGY_PLAIN.get(row["strategy"] or "",
                                      ((row["strategy"] or "Trade").replace("_", " ").title(), ""))
        row["label"] = label
        row["won"] = (row["pnl"] or 0) >= 0
    return to_native({
        "trades": rows,
        "equity": get_equity_series(60),
        "daily":  get_daily_pnl(10),
    })
