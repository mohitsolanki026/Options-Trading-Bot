import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  SIGNAL DEFINITIONS
# ─────────────────────────────────────────

def signal_technical(pcr: float, sentiment: str) -> dict:
    """
    Signal 1: Technical sentiment from PCR.
    Score +1 if clear direction, 0 if neutral.
    """
    if pcr > 1.2:
        return {"score": 1, "label": "Technical", "value": f"PCR {pcr} → Bullish", "bias": "BULLISH"}
    elif pcr < 0.8:
        return {"score": 1, "label": "Technical", "value": f"PCR {pcr} → Bearish", "bias": "BEARISH"}
    else:
        return {"score": 0, "label": "Technical", "value": f"PCR {pcr} → Neutral", "bias": "NEUTRAL"}


def signal_oi(support: float, resistance: float, spot: float) -> dict:
    """
    Signal 2: OI data — where is big money positioned?
    Score +1 if spot is clearly between support and resistance.
    """
    range_size = resistance - support
    if range_size <= 0:
        return {"score": 0, "label": "OI Data", "value": "No clear range", "bias": "NEUTRAL"}

    position = (spot - support) / range_size

    if 0.2 <= position <= 0.8:
        return {"score": 1, "label": "OI Data",
                "value": f"Spot {spot} in OI range [{support}–{resistance}]",
                "bias": "NEUTRAL"}
    elif position < 0.2:
        return {"score": 1, "label": "OI Data",
                "value": f"Spot near support {support} — bounce possible",
                "bias": "BULLISH"}
    else:
        return {"score": 1, "label": "OI Data",
                "value": f"Spot near resistance {resistance} — rejection possible",
                "bias": "BEARISH"}


def signal_pcr_extreme(pcr: float) -> dict:
    """
    Signal 3: PCR at extremes = contrarian signal.
    Very high PCR → market oversold → bounce likely.
    Very low PCR  → market overbought → fall likely.
    """
    if pcr > 1.5:
        return {"score": 1, "label": "PCR Extreme",
                "value": f"PCR {pcr} → Extreme bullish/oversold",
                "bias": "BULLISH"}
    elif pcr < 0.6:
        return {"score": 1, "label": "PCR Extreme",
                "value": f"PCR {pcr} → Extreme bearish/overbought",
                "bias": "BEARISH"}
    else:
        return {"score": 0, "label": "PCR Extreme",
                "value": f"PCR {pcr} → No extreme reading",
                "bias": "NEUTRAL"}


def signal_iv_rank(avg_iv: float, vix: float) -> dict:
    """
    Signal 4: IV Rank — is options premium cheap or expensive?
    High IV → sell options (premium is expensive)
    Low IV  → buy options (premium is cheap)
    """
    # Use VIX as IV rank proxy (we'll add historical IV rank in Week 3)
    if avg_iv > 20 or vix > 18:
        return {"score": 1, "label": "IV Rank",
                "value": f"IV {avg_iv}% / VIX {vix} → High IV, sell premium",
                "bias": "SELL_PREMIUM"}
    elif avg_iv < 12 or vix < 12:
        return {"score": 1, "label": "IV Rank",
                "value": f"IV {avg_iv}% / VIX {vix} → Low IV, buy options",
                "bias": "BUY_OPTIONS"}
    else:
        return {"score": 0, "label": "IV Rank",
                "value": f"IV {avg_iv}% / VIX {vix} → Neutral IV",
                "bias": "NEUTRAL"}


def signal_vix_direction(vix: float) -> dict:
    """
    Signal 5: VIX level and direction.
    VIX falling → good for short premium strategies.
    VIX rising  → dangerous for sellers.
    """
    if vix < 14:
        return {"score": 1, "label": "VIX",
                "value": f"VIX {vix} → Low/calm, safe to sell premium",
                "bias": "SELL_PREMIUM"}
    elif vix > 20:
        return {"score": 1, "label": "VIX",
                "value": f"VIX {vix} → High fear, buy protection",
                "bias": "BUY_OPTIONS"}
    elif 14 <= vix <= 20:
        return {"score": 1, "label": "VIX",
                "value": f"VIX {vix} → Moderate, proceed with caution",
                "bias": "NEUTRAL"}
    else:
        return {"score": 0, "label": "VIX",
                "value": f"VIX {vix} → Uncertain",
                "bias": "NEUTRAL"}


def signal_expiry_theta(days_to_expiry: int, theta: float) -> dict:
    theta = theta or 0.0
    """
    Bonus Signal: Expiry proximity + Theta decay.
    On expiry day, theta is maximum → strong sell signal.
    """
    if days_to_expiry <= 1:
        return {"score": 1, "label": "Theta/Expiry",
                "value": f"Expiry in {days_to_expiry} day(s), Theta=₹{abs(theta)}/day",
                "bias": "SELL_PREMIUM"}
    elif days_to_expiry <= 3:
        return {"score": 1, "label": "Theta/Expiry",
                "value": f"{days_to_expiry} days to expiry — theta accelerating",
                "bias": "SELL_PREMIUM"}
    else:
        return {"score": 0, "label": "Theta/Expiry",
                "value": f"{days_to_expiry} days to expiry — theta slow",
                "bias": "NEUTRAL"}


# ─────────────────────────────────────────
#  CONFLUENCE ENGINE (main function)
# ─────────────────────────────────────────

def run_confluence(
    pcr: float,
    sentiment: str,
    support: float,
    resistance: float,
    nifty_spot: float,
    avg_iv: float,
    vix: float,
    days_to_expiry: int,
    theta: float,
    regime: str,
) -> dict:
    """
    Runs all signals and returns a trade decision.
    Score >= 4 → TAKE TRADE
    Score < 4  → SKIP
    """

    signals = [
        signal_technical(pcr, sentiment),
        signal_oi(support, resistance, nifty_spot),
        signal_pcr_extreme(pcr),
        signal_iv_rank(avg_iv, vix),
        signal_vix_direction(vix),
        signal_expiry_theta(days_to_expiry, theta),
    ]

    total_score   = sum(s["score"] for s in signals)
    max_score     = len(signals)
    go_threshold  = 4  # need at least 4/6 signals

    # Count bias votes
    bias_votes = [s["bias"] for s in signals if s["bias"] != "NEUTRAL"]
    sell_votes = bias_votes.count("SELL_PREMIUM")
    buy_votes  = bias_votes.count("BUY_OPTIONS")
    bull_votes = bias_votes.count("BULLISH")
    bear_votes = bias_votes.count("BEARISH")

    # Overall bias
    if sell_votes >= 2:
        overall_bias = "SELL PREMIUM"
    elif buy_votes >= 2:
        overall_bias = "BUY OPTIONS"
    elif bull_votes > bear_votes:
        overall_bias = "BULLISH"
    elif bear_votes > bull_votes:
        overall_bias = "BEARISH"
    else:
        overall_bias = "NEUTRAL"

    # Decision
    decision = "✅ TAKE TRADE" if total_score >= go_threshold else "❌ SKIP"

    result = {
        "score":        total_score,
        "max_score":    max_score,
        "threshold":    go_threshold,
        "decision":     decision,
        "overall_bias": overall_bias,
        "signals":      signals,
        "regime":       regime,
    }

    logger.info(f"📊 Confluence Score: {total_score}/{max_score} → {decision}")
    return result