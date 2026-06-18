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

logger = logging.getLogger(__name__)


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
    return [
        {"offset": +1, "type": "CE", "action": "SELL"},
        {"offset": -1, "type": "PE", "action": "SELL"},
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

    if bias == "SELL PREMIUM":
        return "short_straddle"
    if bias == "BUY OPTIONS":
        return "long_straddle"
    if bias == "BULLISH":
        return "bull_call_spread"
    if bias == "BEARISH":
        return "bear_put_spread"
    # Neutral / range-bound → sell premium is the house edge
    return "short_straddle"


# ─────────────────────────────────────────
#  PRICE / TOKEN RESOLUTION
# ─────────────────────────────────────────

def _resolve_leg(strike: float, opt_type: str, df_oi, options_df) -> dict:
    """Return {ltp, token, symbol} for a strike+type, or None if unavailable."""
    ltp = 0.0
    if df_oi is not None and not df_oi.empty:
        row = df_oi[df_oi["strike"] == strike]
        if not row.empty:
            ltp = float(row.iloc[0].get(f"{opt_type}_LTP", 0) or 0)

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
    return {"ltp": ltp, "token": token, "symbol": symbol}


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
) -> dict:
    """
    Build a concrete, priced position for `strategy`.
    Returns a position dict, or None if any leg could not be priced.
    """
    key = normalise_strategy(strategy)
    if key is None:
        logger.warning(f"⚠️ Unknown strategy '{strategy}' — cannot build position.")
        return None

    builder, sl_pct, target_pct = STRATEGIES[key]
    atm_strike = summary["atm_strike"]
    # Infer strike gap from the chain (spacing between adjacent strikes)
    strike_gap = _infer_strike_gap(df_oi, fallback=50)

    legs = []
    for abstract in builder(summary):
        strike = atm_strike + abstract["offset"] * strike_gap
        resolved = _resolve_leg(strike, abstract["type"], df_oi, options_df)
        if resolved is None:
            logger.warning(
                f"⚠️ {index} {key}: could not price {abstract['type']} @ {strike}"
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
    premium_value = abs(net_credit) * lot_size * lots
    stop_loss_pnl = round(-sl_pct * premium_value, 2)
    target_pnl    = round(+target_pct * premium_value, 2)

    direction = "SELL" if net_credit >= 0 else "BUY"

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
