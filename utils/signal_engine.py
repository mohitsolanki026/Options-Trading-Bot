"""
Confluence / signal engine + the code-level entry gate.

Design principles (vs the old version):
  * Every signal must return 0 when it carries NO edge. The old vix/oi signals
    fired for essentially every input, inflating the score so "4/7" was a rubber
    stamp. Signals now only score when they say something.
  * No double-counting. PCR was counted twice (technical + extreme); it is now a
    single weighted signal.
  * Signals are WEIGHTED, not equal 1-point votes — IV rank and multi-indicator
    TA (the real edges) count more.
  * Entry requires DIRECTIONAL/VOL CONVICTION (bias_confirmed), not just a count.
  * Premium-selling is only allowed when IV is genuinely rich (IV rank), so we
    don't sell cheap volatility.
"""

import logging

logger = logging.getLogger(__name__)

# Weighted score needed to consider a trade (out of MAX_SCORE below).
ENTRY_THRESHOLD = 4
# Minimum weighted margin between opposing biases to call a direction "confirmed".
BIAS_MARGIN = 2
# IV rank (0-100) at/above which selling premium has edge; below LO → buy edge.
IV_RANK_SELL = 55
IV_RANK_BUY  = 30


# ─────────────────────────────────────────
#  SIGNALS  (each returns {weight-scored, label, value, bias})
#  bias ∈ {BULLISH, BEARISH, SELL_PREMIUM, BUY_OPTIONS, NEUTRAL}
# ─────────────────────────────────────────

def signal_pcr(pcr: float) -> dict:
    """Single PCR signal. Extremes weigh more; mid-range carries no edge → 0."""
    if pcr >= 1.5:
        return {"score": 2, "label": "PCR", "value": f"{pcr} extreme → bullish/oversold", "bias": "BULLISH"}
    if pcr >= 1.2:
        return {"score": 1, "label": "PCR", "value": f"{pcr} → bullish", "bias": "BULLISH"}
    if pcr <= 0.6:
        return {"score": 2, "label": "PCR", "value": f"{pcr} extreme → bearish/overbought", "bias": "BEARISH"}
    if pcr <= 0.8:
        return {"score": 1, "label": "PCR", "value": f"{pcr} → bearish", "bias": "BEARISH"}
    return {"score": 0, "label": "PCR", "value": f"{pcr} neutral", "bias": "NEUTRAL"}


def signal_oi_position(support: float, resistance: float, spot: float) -> dict:
    """
    OI structure. ONLY scores when spot is pressed against support/resistance
    (a real bounce/rejection edge). Mid-range = no edge → 0 (was wrongly +1).
    """
    rng = resistance - support
    if rng <= 0:
        return {"score": 0, "label": "OI", "value": "no clear range", "bias": "NEUTRAL"}
    pos = (spot - support) / rng
    if pos < 0.2:
        return {"score": 1, "label": "OI", "value": f"near support {support} → bounce", "bias": "BULLISH"}
    if pos > 0.8:
        return {"score": 1, "label": "OI", "value": f"near resistance {resistance} → rejection", "bias": "BEARISH"}
    return {"score": 0, "label": "OI", "value": f"mid-range ({round(pos,2)}) — no edge", "bias": "NEUTRAL"}


def signal_iv_rank(iv_rank) -> dict:
    """
    REAL IV rank (0-100) vs trailing history — the core edge for premium trades.
    Weighted 2. Unknown (insufficient history) → 0, no edge claimed.
    """
    if iv_rank is None:
        return {"score": 0, "label": "IV Rank", "value": "insufficient history", "bias": "NEUTRAL"}
    if iv_rank >= IV_RANK_SELL:
        return {"score": 2, "label": "IV Rank", "value": f"{iv_rank} rich → sell premium", "bias": "SELL_PREMIUM"}
    if iv_rank <= IV_RANK_BUY:
        return {"score": 2, "label": "IV Rank", "value": f"{iv_rank} cheap → buy options", "bias": "BUY_OPTIONS"}
    return {"score": 0, "label": "IV Rank", "value": f"{iv_rank} mid — no edge", "bias": "NEUTRAL"}


def signal_vix(vix: float) -> dict:
    """VIX only carries edge at extremes; the calm/normal middle scores 0."""
    if vix is None:
        return {"score": 0, "label": "VIX", "value": "n/a", "bias": "NEUTRAL"}
    if vix < 12:
        return {"score": 1, "label": "VIX", "value": f"{vix} very calm → sell premium", "bias": "SELL_PREMIUM"}
    if vix > 22:
        return {"score": 1, "label": "VIX", "value": f"{vix} fear → buy protection / avoid sells", "bias": "BUY_OPTIONS"}
    return {"score": 0, "label": "VIX", "value": f"{vix} normal — no edge", "bias": "NEUTRAL"}


def signal_theta_expiry(days_to_expiry: int) -> dict:
    """Near expiry, accelerating theta favours short premium."""
    if days_to_expiry is not None and days_to_expiry <= 2:
        return {"score": 1, "label": "Theta", "value": f"{days_to_expiry}d to expiry → theta", "bias": "SELL_PREMIUM"}
    return {"score": 0, "label": "Theta", "value": f"{days_to_expiry}d — slow theta", "bias": "NEUTRAL"}


def signal_ta(ta: dict) -> dict:
    """Multi-indicator TA agreement (weighted 2). Needs 3+ indicators aligned."""
    if not ta or not ta.get("signals"):
        return {"score": 0, "label": "TA", "value": "no TA", "bias": "NEUTRAL"}
    bull = ta.get("bull_count", 0)
    bear = ta.get("bear_count", 0)
    overall = ta.get("overall", "NEUTRAL")
    rsi = ta.get("rsi", 50)
    if bull >= 3:
        return {"score": 2, "label": "TA", "value": f"{overall} RSI={rsi}", "bias": "BULLISH"}
    if bear >= 3:
        return {"score": 2, "label": "TA", "value": f"{overall} RSI={rsi}", "bias": "BEARISH"}
    return {"score": 0, "label": "TA", "value": f"mixed ({bull}🟢/{bear}🔴)", "bias": "NEUTRAL"}


# Max achievable weighted score = 2+1+2+1+1+2 = 9
MAX_SCORE = 9


# ─────────────────────────────────────────
#  CONFLUENCE ENGINE
# ─────────────────────────────────────────

def run_confluence(
    pcr: float,
    support: float,
    resistance: float,
    nifty_spot: float,
    vix: float,
    days_to_expiry: int,
    regime: str,
    iv_rank=None,
    ta: dict = None,
    # kept for backwards-compat with old callers (ignored):
    sentiment: str = None, avg_iv: float = None, theta: float = None,
) -> dict:
    """
    Run all signals → weighted score, dominant bias, and a conviction flag.
    Returns dict with: score, max_score, threshold, overall_bias, bias_confirmed,
    iv_rank, premium_sell_ok, signals, regime.
    """
    signals = [
        signal_pcr(pcr),
        signal_oi_position(support, resistance, nifty_spot),
        signal_iv_rank(iv_rank),
        signal_vix(vix),
        signal_theta_expiry(days_to_expiry),
        signal_ta(ta or {}),
    ]

    total_score = sum(s["score"] for s in signals)

    # Weighted votes per bias
    votes = {"BULLISH": 0, "BEARISH": 0, "SELL_PREMIUM": 0, "BUY_OPTIONS": 0}
    for s in signals:
        if s["bias"] in votes:
            votes[s["bias"]] += s["score"]

    dir_margin = abs(votes["BULLISH"] - votes["BEARISH"])
    vol_margin = abs(votes["SELL_PREMIUM"] - votes["BUY_OPTIONS"])

    # Dominant bias = the strongest confirmed camp (vol vs directional)
    if vol_margin >= dir_margin and (votes["SELL_PREMIUM"] or votes["BUY_OPTIONS"]):
        overall_bias = "SELL_PREMIUM" if votes["SELL_PREMIUM"] >= votes["BUY_OPTIONS"] else "BUY_OPTIONS"
        margin = vol_margin
    elif votes["BULLISH"] or votes["BEARISH"]:
        overall_bias = "BULLISH" if votes["BULLISH"] >= votes["BEARISH"] else "BEARISH"
        margin = dir_margin
    else:
        overall_bias = "NEUTRAL"
        margin = 0

    bias_confirmed = margin >= BIAS_MARGIN
    # Selling premium is only OK when IV is genuinely rich.
    premium_sell_ok = (iv_rank is not None and iv_rank >= IV_RANK_SELL)

    decision = "✅ TAKE TRADE" if total_score >= ENTRY_THRESHOLD and bias_confirmed else "❌ SKIP"

    result = {
        "score":           total_score,
        "max_score":       MAX_SCORE,
        "threshold":       ENTRY_THRESHOLD,
        "decision":        decision,
        "overall_bias":    overall_bias,
        "bias_confirmed":  bias_confirmed,
        "bias_margin":     margin,
        "iv_rank":         iv_rank,
        "premium_sell_ok": premium_sell_ok,
        "votes":           votes,
        "signals":         signals,
        "regime":          regime,
    }
    logger.info(
        f"📊 Confluence {total_score}/{MAX_SCORE} bias={overall_bias} "
        f"(margin {margin}, confirmed={bias_confirmed}) IVR={iv_rank} → {decision}"
    )
    return result


# ─────────────────────────────────────────
#  CODE-LEVEL ENTRY GATE
# ─────────────────────────────────────────

def entry_allowed(
    confluence: dict,
    risk_status: dict,
    open_count: int,
    max_positions: int,
    in_entry_window: bool,
    blackout=(False, ""),
    in_cooldown=(False, ""),
    correlation=(True, ""),
) -> tuple[bool, str]:
    """
    Authoritative GO/NO-GO for any new trade — enforced in code regardless of the
    LLM. The LLM may only refine *which* strategy within what this permits.

    blackout / in_cooldown : (is_blocked, reason) tuples from event_calendar /
    paper_trader. correlation : (is_ok, reason) from the correlation check.
    Returns (allowed, reason).
    """
    score     = confluence.get("score", 0)
    threshold = confluence.get("threshold", ENTRY_THRESHOLD)
    max_score = confluence.get("max_score", "?")

    if score < threshold:
        return False, f"Score {score}/{max_score} < {threshold}"
    if not confluence.get("bias_confirmed", False):
        return False, f"Bias not confirmed (margin {confluence.get('bias_margin', 0)})"
    # Don't sell cheap volatility.
    if confluence.get("overall_bias") == "SELL_PREMIUM" and not confluence.get("premium_sell_ok", False):
        return False, f"IV rank too low to sell premium (IVR {confluence.get('iv_rank')})"
    if risk_status.get("trading_halted"):
        return False, f"Trading halted: {risk_status.get('halt_reason')}"
    if risk_status.get("headroom", 1) <= 0:
        return False, "Daily loss limit reached"
    if open_count >= max_positions:
        return False, f"Max positions reached: {open_count}/{max_positions}"
    if not in_entry_window:
        return False, "Outside safe entry window"

    blocked, reason = blackout
    if blocked:
        return False, f"Event/expiry blackout: {reason}"
    cooling, reason = in_cooldown
    if cooling:
        return False, f"Post-exit cooldown: {reason}"
    ok, reason = correlation
    if not ok:
        return False, f"Correlated exposure: {reason}"

    return True, "OK"
