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

from utils.greeks_engine import calculate_greeks
from config.settings import (
    MIN_LEG_OI, MIN_CREDIT_PCT, STRANGLE_TARGET_DELTA,
)

logger = logging.getLogger(__name__)
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
        {"offset": +1, "type": "CE", "action": "SELL", "target_delta": STRANGLE_TARGET_DELTA},
        {"offset": -1, "type": "PE", "action": "SELL", "target_delta": STRANGLE_TARGET_DELTA},
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
STRATEGIES = {
    "short_straddle":   (_short_straddle,   0.40, 0.50),
    "short_strangle":   (_short_strangle,   0.40, 0.50),
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
    """Map a free-text strategy name to a canonical registry key (or None)."""
    if not name:
        return None
    key = name.strip().lower()
    if key in STRATEGIES:
        return key
    return ALIASES.get(key)


# ─────────────────────────────────────────
#  STRATEGY SELECTION (code is the authority)
# ─────────────────────────────────────────

def select_strategy(confluence: dict, regime: dict, decision: dict = None) -> str:
    """
    Choose a strategy in CODE based on confluence bias + regime.
    If the LLM suggested a strategy that is in the allowed registry, honour it;
    otherwise fall back to the deterministic rule. The LLM is advisory only.
    """
    # Honour a valid LLM suggestion
    if decision:
        suggested = normalise_strategy(decision.get("strategy", ""))
        if suggested:
            return suggested

    bias = (confluence or {}).get("overall_bias", "NEUTRAL")

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
    min_oi: int = MIN_LEG_OI,
    min_credit_pct: float = MIN_CREDIT_PCT,
) -> dict:
    """
    Build a concrete, priced position for `strategy`.

    Returns a position dict, or None if a leg can't be priced, a leg is illiquid
    (OI < min_oi), or a short-premium trade's net credit is too thin
    (< min_credit_pct of spot). When `greeks` is supplied, legs flagged with a
    target_delta are strike-selected by delta instead of a fixed offset.
    """
    key = normalise_strategy(strategy)
    if key is None:
        logger.warning(f"⚠️ Unknown strategy '{strategy}' — cannot build position.")
        return None

    builder, sl_pct, target_pct = STRATEGIES[key]
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
        "stop_loss_pnl":  stop_loss_pnl,
        "target_pnl":     target_pnl,
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


def current_price_map(position: dict, tick_store=None, df_oi=None) -> dict:
    """
    Build {token: ltp} for a position, preferring live ticks, then the latest
    options-chain snapshot (df_oi). Legs with no price are omitted (callers fall
    back to entry price → flat on that leg).
    """
    prices = {}
    for leg in position["legs"]:
        ltp = tick_store.get_ltp(leg["token"]) if tick_store else 0
        if not ltp:
            ltp = _lookup_df_oi(df_oi, leg)
        if ltp:
            prices[leg["token"]] = ltp
    return prices


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


def realise_pnl(position: dict, price_map: dict) -> float:
    """Compute final P&L and stamp exit_ltp on each leg. Mutates position legs."""
    lot_size = position["lot_size"]
    total = 0.0
    combined_exit = 0.0
    for leg in position["legs"]:
        cur = price_map.get(leg["token"]) or leg["entry_ltp"]
        leg["exit_ltp"] = cur
        combined_exit += cur
        total += _sign(leg) * lot_size * leg["lots"] * (leg["entry_ltp"] - cur)
    position["exit_combined"] = round(combined_exit, 2)
    return round(total, 2)


def check_levels(position: dict, price_map: dict) -> str:
    """Return 'STOP_LOSS' | 'TARGET' | 'HOLD' based on unrealised P&L thresholds."""
    pnl = unrealised_pnl(position, price_map)
    if pnl <= position["stop_loss_pnl"]:
        return "STOP_LOSS"
    if pnl >= position["target_pnl"]:
        return "TARGET"
    return "HOLD"
