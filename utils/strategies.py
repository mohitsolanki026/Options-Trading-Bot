"""
Strategy-agnostic options execution engine.

Every trade is represented as a list of LEGS rather than a hardcoded straddle.
This module knows how to:
  1. Build the legs for a named strategy (short straddle, spreads, directional…).
  2. Resolve each leg's instrument token + entry LTP from the live options chain.
  3. Compute generic mark-to-market P&L for any combination of legs.

P&L convention (per leg, per unit):
    SELL leg pnl = (entry_ltp - exit_ltp)   # profit when premium falls
    BUY  leg pnl = (exit_ltp - entry_ltp)   # profit when premium rises
Total P&L = Σ_legs sign * lot_size * lots * (entry_ltp - current_ltp)
where sign = +1 for SELL, -1 for BUY.

Stop-loss / target are stored as ABSOLUTE P&L thresholds (₹), so "higher is
better" holds for every strategy and the monitors stay strategy-agnostic.
"""

import logging
import os
import re

from utils import settings_store
from utils.greeks_engine import calculate_greeks
from config.settings import (
    MIN_LEG_OI, MIN_CREDIT_PCT, STRANGLE_TARGET_DELTA,
)

logger = logging.getLogger(__name__)


def _tuned(key, fallback):
    """A dashboard override if the user set one, else the env-derived constant."""
    try:
        value = settings_store.override(key)
    except Exception:
        return fallback
    return fallback if value is None else value
RISK_FREE_RATE = 0.065


# ─────────────────────────────────────────
#  STRATEGY REGISTRY
# ─────────────────────────────────────────
# Each builder returns a list of abstract legs:
#   {"offset": <int strike steps from ATM>, "type": "CE"|"PE", "action": "BUY"|"SELL"}
# sl_pct / target_pct are fractions of the net premium magnitude used to derive
# the absolute ₹ stop-loss and target thresholds.

def _short_straddle(_):
    return [
        {"offset": 0, "type": "CE", "action": "SELL"},
        {"offset": 0, "type": "PE", "action": "SELL"},
    ]


def _short_strangle(_):
    # Strikes chosen by delta when greeks are available (else ±1 strike fallback).
    return [
        {"offset": +1, "type": "CE", "action": "SELL",
         "target_delta": _tuned("strangle_target_delta", STRANGLE_TARGET_DELTA)},
        {"offset": -1, "type": "PE", "action": "SELL",
         "target_delta": _tuned("strangle_target_delta", STRANGLE_TARGET_DELTA)},
    ]


def _long_straddle(_):
    return [
        {"offset": 0, "type": "CE", "action": "BUY"},
        {"offset": 0, "type": "PE", "action": "BUY"},
    ]


def _long_ce(_):
    return [{"offset": 0, "type": "CE", "action": "BUY"}]


def _long_pe(_):
    return [{"offset": 0, "type": "PE", "action": "BUY"}]


def _bull_call_spread(_):
    return [
        {"offset": 0,  "type": "CE", "action": "BUY"},
        {"offset": +1, "type": "CE", "action": "SELL"},
    ]


def _bear_put_spread(_):
    return [
        {"offset": 0,  "type": "PE", "action": "BUY"},
        {"offset": -1, "type": "PE", "action": "SELL"},
    ]


# name -> (leg_builder, sl_pct, target_pct)
#
# Sold premium: the stop is 100% of the credit (the premium has doubled) and
# the target is 50% decay. The old 40% stop was hit by ordinary intraday noise
# long before any decay could arrive, which is exactly the pattern the journal
# showed: many small stop-outs, targets almost never reached. Both numbers are
# tunable from the dashboard; these are the fallbacks.
SHORT_STOP_PCT   = float(os.getenv("SHORT_PREMIUM_STOP_PCT", 1.0))
SHORT_TARGET_PCT = float(os.getenv("SHORT_PREMIUM_TARGET_PCT", 0.5))

STRATEGIES = {
    "short_straddle":   (_short_straddle,   None, None),    # None → live setting
    "short_strangle":   (_short_strangle,   None, None),
    "long_straddle":    (_long_straddle,    0.40, 0.60),
    "long_ce":          (_long_ce,          0.40, 0.60),
    "long_pe":          (_long_pe,          0.40, 0.60),
    "bull_call_spread": (_bull_call_spread, 0.50, 0.80),
    "bear_put_spread":  (_bear_put_spread,  0.50, 0.80),
}

# Human-readable aliases the LLM might emit -> canonical key
ALIASES = {
    "short straddle":   "short_straddle",
    "straddle":         "short_straddle",
    "short strangle":   "short_strangle",
    "strangle":         "short_strangle",
    "long straddle":    "long_straddle",
    "buy ce":           "long_ce",
    "long call":        "long_ce",
    "buy pe":           "long_pe",
    "long put":         "long_pe",
    "bull call spread": "bull_call_spread",
    "bear put spread":  "bear_put_spread",
}


def normalise_strategy(name: str) -> str:
    """
    Map a free-text strategy name to a canonical registry key (or None).

    The LLM writes names like "Short Straddle 24150". The trailing strike used
    to make every suggestion unrecognisable, so none was ever honoured.
    """
    if not name:
        return None
    key = name.strip().lower()
    if key in STRATEGIES:
        return key
    key = re.sub(r"[\s\d,./@-]+$", "", key).strip()      # drop a strike suffix
    if key in STRATEGIES:
        return key
    return ALIASES.get(key)


# Which strategies fit which confirmed view. An LLM suggestion outside its
# row is ignored: a "short straddle" when the gate found a bearish edge would
# be a different trade from the one the checks approved.
COMPATIBLE = {
    "SELL_PREMIUM": ("short_strangle", "short_straddle"),
    "BUY_OPTIONS":  ("long_straddle",),
    "BULLISH":      ("bull_call_spread", "long_ce"),
    "BEARISH":      ("bear_put_spread", "long_pe"),
}


# ─────────────────────────────────────────
#  STRATEGY SELECTION (code is the authority)
# ─────────────────────────────────────────

def select_strategy(confluence: dict, regime: dict, decision: dict = None) -> str:
    """
    Choose a strategy in CODE based on confluence bias + regime.
    If the LLM suggested a strategy that is in the allowed registry, honour it;
    otherwise fall back to the deterministic rule. The LLM is advisory only.
    """
    bias = (confluence or {}).get("overall_bias", "NEUTRAL")

    # Honour an LLM suggestion only when it expresses the same view the
    # checks confirmed.
    if decision:
        suggested = normalise_strategy(decision.get("strategy", ""))
        if suggested and suggested in COMPATIBLE.get(bias, ()):
            return suggested
        if suggested:
            logger.info(f"ℹ️ LLM suggested {suggested}, which does not fit a "
                        f"{bias} view — using the default for that view.")

    if bias == "SELL_PREMIUM":
        return "short_strangle"   # defined OTM short vol, delta-selected strikes
    if bias == "BUY_OPTIONS":
        return "long_straddle"
    if bias == "BULLISH":
        return "bull_call_spread"
    if bias == "BEARISH":
        return "bear_put_spread"
    # Neutral shouldn't reach here (gate requires confirmed bias); safe default.
    return "short_straddle"


# ─────────────────────────────────────────
#  PRICE / TOKEN RESOLUTION
# ─────────────────────────────────────────

def _resolve_leg(strike: float, opt_type: str, df_oi, options_df, min_oi: int = 0) -> dict:
    """Return {ltp, token, symbol} for a strike+type, or None if unavailable/illiquid."""
    ltp = 0.0
    oi  = 0
    if df_oi is not None and not df_oi.empty:
        row = df_oi[df_oi["strike"] == strike]
        if not row.empty:
            ltp = float(row.iloc[0].get(f"{opt_type}_LTP", 0) or 0)
            oi  = float(row.iloc[0].get(f"{opt_type}_OI", 0) or 0)

    token = None
    symbol = None
    if options_df is not None and not options_df.empty:
        mask = (
            (options_df["strike"] == strike) &
            (options_df["symbol"].str.endswith(opt_type))
        )
        contract = options_df[mask]
        if not contract.empty:
            token = str(contract.iloc[0]["token"])
            symbol = str(contract.iloc[0]["symbol"])

    if ltp <= 0 or token is None:
        return None
    if min_oi and oi < min_oi:
        logger.warning(f"⚠️ {opt_type} {int(strike)} illiquid (OI {int(oi)} < {min_oi}) — skip.")
        return None
    return {"ltp": ltp, "token": token, "symbol": symbol}


def _select_strike_by_delta(strikes, target_delta, opt_type, spot, T, sigma) -> float:
    """Pick the strike whose Black-Scholes |delta| is closest to target_delta."""
    best, best_diff = None, 1e9
    for K in strikes:
        g = calculate_greeks(spot, K, T, RISK_FREE_RATE, sigma, opt_type)
        d = abs(g.get("delta") or 0)
        diff = abs(d - target_delta)
        if diff < best_diff:
            best_diff, best = diff, K
    return best


def build_position(
    index: str,
    strategy: str,
    summary: dict,
    df_oi,
    options_df,
    lots: int,
    lot_size: int,
    expiry: str,
    trade_id: int = 0,
    greeks: dict = None,
    min_oi: int = None,
    min_credit_pct: float = None,
) -> dict:
    """
    Build a concrete, priced position for `strategy`.

    Returns a position dict, or None if a leg can't be priced, a leg is illiquid
    (OI < min_oi), or a short-premium trade's net credit is too thin
    (< min_credit_pct of spot). When `greeks` is supplied, legs flagged with a
    target_delta are strike-selected by delta instead of a fixed offset.
    """
    if min_oi is None:
        min_oi = _tuned("min_leg_oi", MIN_LEG_OI)
    if min_credit_pct is None:
        min_credit_pct = _tuned("min_credit_pct", MIN_CREDIT_PCT)

    key = normalise_strategy(strategy)
    if key is None:
        logger.warning(f"⚠️ Unknown strategy '{strategy}' — cannot build position.")
        return None

    builder, sl_pct, target_pct = STRATEGIES[key]
    if sl_pct is None:
        sl_pct = _tuned("short_premium_stop_pct", SHORT_STOP_PCT)
    if target_pct is None:
        target_pct = _tuned("short_premium_target_pct", SHORT_TARGET_PCT)
    atm_strike = summary["atm_strike"]
    spot       = summary.get("nifty_spot", atm_strike)
    strike_gap = _infer_strike_gap(df_oi, fallback=50)

    # Delta-selection inputs (only used for target_delta legs)
    dte   = (greeks or {}).get("days_to_exp")
    T     = max((dte or 0), 0) / 365 if dte is not None else 0
    sigma = float((greeks or {}).get("avg_iv") or 0) / 100
    all_strikes = sorted(df_oi["strike"].unique()) if df_oi is not None and not df_oi.empty else []

    legs = []
    for abstract in builder(summary):
        strike = atm_strike + abstract["offset"] * strike_gap
        # Prefer delta-based strike selection when we have the inputs
        if abstract.get("target_delta") and T > 0 and sigma > 0 and all_strikes:
            picked = _select_strike_by_delta(
                all_strikes, abstract["target_delta"], abstract["type"], spot, T, sigma
            )
            if picked is not None:
                strike = picked
        resolved = _resolve_leg(strike, abstract["type"], df_oi, options_df, min_oi=min_oi)
        if resolved is None:
            logger.warning(
                f"⚠️ {index} {key}: could not price/illiquid {abstract['type']} @ {strike}"
            )
            return None
        legs.append({
            "strike":      strike,
            "option_type": abstract["type"],
            "action":      abstract["action"],
            "token":       resolved["token"],
            "symbol":      resolved["symbol"],
            "lots":        lots,
            "entry_ltp":   resolved["ltp"],
            "exit_ltp":    None,
        })

    # Net premium magnitude per unit (credit positive, debit negative)
    net_credit = sum(_sign(l) * l["entry_ltp"] for l in legs)
    direction = "SELL" if net_credit >= 0 else "BUY"

    # Minimum-credit filter: don't sell thin premium (full tail risk, no reward)
    if direction == "SELL" and min_credit_pct and spot:
        if net_credit < min_credit_pct * spot:
            logger.warning(
                f"⚠️ {index} {key}: credit ₹{net_credit:.1f} < "
                f"{min_credit_pct*100:.2f}% of spot — skip thin premium."
            )
            return None

    # Vol/gamma-aware SL & target: tighten near expiry (gamma risk spikes).
    if dte is not None and dte <= 1:
        sl_pct, target_pct = sl_pct * 0.8, target_pct * 0.7

    premium_value = abs(net_credit) * lot_size * lots
    stop_loss_pnl = round(-sl_pct * premium_value, 2)
    target_pnl    = round(+target_pct * premium_value, 2)

    return {
        "id":             trade_id,
        "index":          index,
        "strategy":       key,
        "legs":           legs,
        "lot_size":       lot_size,
        "lots":           lots,
        "expiry":         expiry,
        "net_credit":     round(net_credit, 2),
        "entry_combined": round(sum(l["entry_ltp"] for l in legs), 2),
        "direction":      direction,
        "sl_pct":         sl_pct,
        "target_pct":     target_pct,
        "stop_loss_pnl":  stop_loss_pnl,
        "target_pnl":     target_pnl,
        "priced_from":    "snapshot",
        "status":         "OPEN",
        "exit_combined":  None,
        "pnl":            None,
        "exit_reason":    None,
    }


def _sign(leg: dict) -> int:
    return 1 if leg["action"] == "SELL" else -1


def _infer_strike_gap(df_oi, fallback: int = 50) -> int:
    if df_oi is None or df_oi.empty or len(df_oi) < 2:
        return fallback
    strikes = sorted(df_oi["strike"].unique())
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return int(min(gaps)) if gaps else fallback


# ─────────────────────────────────────────
#  P&L
# ─────────────────────────────────────────

def _lookup_df_oi(df_oi, leg: dict) -> float:
    if df_oi is None or df_oi.empty:
        return 0.0
    row = df_oi[df_oi["strike"] == leg["strike"]]
    if row.empty:
        return 0.0
    return float(row.iloc[0].get(f"{leg['option_type']}_LTP", 0) or 0)


# A tick older than this is not a price any more. Two minutes is long for a
# liquid index option and short enough to notice a feed that has gone quiet.
TICK_MAX_AGE_SEC = 120


def current_price_map(position: dict, tick_store=None, df_oi=None,
                      max_tick_age: float = None) -> dict:
    """
    Build {token: ltp} for a position, preferring live ticks, then the latest
    options-chain snapshot (df_oi). Legs with no price are omitted; use
    ``price_map_complete`` before trusting a stop or target on the result.
    """
    prices = {}
    for leg in position["legs"]:
        ltp = tick_store.get_ltp(leg["token"], max_age=max_tick_age) if tick_store else 0
        if not ltp:
            ltp = _lookup_df_oi(df_oi, leg)
        if ltp:
            prices[leg["token"]] = ltp
    return prices


def price_map_complete(position: dict, price_map: dict) -> bool:
    """True when every leg has a price. Stops and targets need all of them."""
    return all(price_map.get(leg["token"]) for leg in position["legs"])


def missing_legs(position: dict, price_map: dict) -> list:
    return [f"{leg['option_type']} {int(leg['strike'])}"
            for leg in position["legs"] if not price_map.get(leg["token"])]


def _slip(price: float, action: str, pct: float, entering: bool) -> float:
    """
    Worsen a fill by ``pct`` of the price, floored at one tick of ₹0.05.
    Selling gets less, buying pays more, whichever way the trade is going.
    """
    if not pct or price <= 0:
        return round(price, 2)
    move = max(price * pct, 0.05)
    # entering a SELL leg or exiting a BUY leg means we are selling → receive less
    selling = (action == "SELL") if entering else (action == "BUY")
    return round(price - move if selling else price + move, 2)


def reprice_entry(position: dict, fresh: dict, slippage_pct: float = 0.0) -> dict:
    """
    Replace snapshot entry prices with fresh ones and apply entry slippage, then
    recompute credit, direction, and the stop and target that hang off them.

    Before this the paper fill used the option chain fetched at the start of
    the scan, up to half a minute earlier, which near expiry is a different
    price. ``fresh`` is ``{token: ltp}``; legs with no fresh price keep the
    snapshot price and are noted on the position.
    """
    used_fresh = 0
    for leg in position["legs"]:
        leg["snapshot_ltp"] = leg["entry_ltp"]
        price = fresh.get(leg["token"])
        if price:
            leg["entry_ltp"] = float(price)
            used_fresh += 1
        leg["entry_ltp"] = _slip(leg["entry_ltp"], leg["action"], slippage_pct, entering=True)

    legs      = position["legs"]
    lot_size  = position["lot_size"]
    lots      = position["lots"]
    net_credit = sum(_sign(l) * l["entry_ltp"] for l in legs)
    premium_value = abs(net_credit) * lot_size * lots
    position["net_credit"]     = round(net_credit, 2)
    position["direction"]      = "SELL" if net_credit >= 0 else "BUY"
    position["entry_combined"] = round(sum(l["entry_ltp"] for l in legs), 2)
    position["stop_loss_pnl"]  = round(-position["sl_pct"] * premium_value, 2)
    position["target_pnl"]     = round(+position["target_pct"] * premium_value, 2)
    position["slippage_pct"]   = slippage_pct
    position["priced_from"]    = ("live" if used_fresh == len(legs)
                                  else "partly-live" if used_fresh else "snapshot")
    return position


def all_tokens(position: dict) -> list:
    """Instrument tokens for every leg — for WebSocket subscription."""
    return [leg["token"] for leg in position["legs"]]


def unrealised_pnl(position: dict, price_map: dict) -> float:
    """
    Mark-to-market P&L (₹) for an open position given {token: current_ltp}.
    Falls back to a leg's entry price if its current price is missing.
    """
    lot_size = position["lot_size"]
    total = 0.0
    for leg in position["legs"]:
        cur = price_map.get(leg["token"])
        if not cur:
            cur = leg["entry_ltp"]   # no fresh price → assume flat on this leg
        total += _sign(leg) * lot_size * leg["lots"] * (leg["entry_ltp"] - cur)
    return round(total, 2)


def realise_pnl(position: dict, price_map: dict, slippage_pct: float = 0.0,
                charges: float = 0.0) -> float:
    """
    Compute final P&L and stamp exit_ltp on each leg. Mutates position legs.

    Exit slippage worsens each leg's fill and ``charges`` (brokerage, STT and
    exchange fees for the whole round trip) is deducted, so the paper result
    is closer to what a real account would have kept.
    """
    lot_size = position["lot_size"]
    total = 0.0
    combined_exit = 0.0
    for leg in position["legs"]:
        cur = price_map.get(leg["token"]) or leg["entry_ltp"]
        cur = _slip(float(cur), leg["action"], slippage_pct, entering=False)
        leg["exit_ltp"] = cur
        combined_exit += cur
        total += _sign(leg) * lot_size * leg["lots"] * (leg["entry_ltp"] - cur)
    total -= float(charges or 0.0)
    position["exit_combined"] = round(combined_exit, 2)
    position["charges"]       = round(float(charges or 0.0), 2)
    position["gross_pnl"]     = round(total + float(charges or 0.0), 2)
    return round(total, 2)


def round_trip_charges(position: dict, per_order: float) -> float:
    """Flat charges for every leg, in and out."""
    return round(float(per_order or 0.0) * len(position["legs"]) * 2, 2)


def check_levels(position: dict, price_map: dict) -> str:
    """Return 'STOP_LOSS' | 'TARGET' | 'HOLD' based on unrealised P&L thresholds."""
    pnl = unrealised_pnl(position, price_map)
    if pnl <= position["stop_loss_pnl"]:
        return "STOP_LOSS"
    if pnl >= position["target_pnl"]:
        return "TARGET"
    return "HOLD"
