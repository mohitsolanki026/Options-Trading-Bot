import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  REGIME DEFINITIONS
# ─────────────────────────────────────────

REGIMES = {
    "TRENDING_UP":    "Trending Up",
    "TRENDING_DOWN":  "Trending Down",
    "SIDEWAYS":       "Sideways",
    "HIGH_VOL":       "High Volatility",
    "EXPIRY":         "Expiry Day",
}

REGIME_STRATEGY = {
    "TRENDING_UP":   "Bull Call Spread",
    "TRENDING_DOWN": "Bear Put Spread",
    "SIDEWAYS":      "Short Straddle / Iron Condor",
    "HIGH_VOL":      "Buy Straddle / Strangle",
    "EXPIRY":        "Short Straddle + Hedge",
}

REGIME_EMOJI = {
    "TRENDING_UP":   "📈",
    "TRENDING_DOWN": "📉",
    "SIDEWAYS":      "↔️",
    "HIGH_VOL":      "🌪️",
    "EXPIRY":        "⏰",
}


# ─────────────────────────────────────────
#  REGIME DETECTOR
# ─────────────────────────────────────────

def detect_regime(
    vix: float,
    pcr: float,
    days_to_expiry: int,
    nifty_spot: float,
    support: float,
    resistance: float,
    avg_iv: float,
) -> dict:
    """
    Detects current market regime based on multiple inputs.

    Rules (in priority order):
    1. Expiry day     → EXPIRY
    2. VIX > 20       → HIGH_VOL
    3. PCR > 1.2      → TRENDING_UP
    4. PCR < 0.8      → TRENDING_DOWN
    5. Spot in range  → SIDEWAYS
    """

    scores = {
        "TRENDING_UP":   0,
        "TRENDING_DOWN": 0,
        "SIDEWAYS":      0,
        "HIGH_VOL":      0,
        "EXPIRY":        0,
    }

    reasons = []

    # --- Rule 1: Expiry day (highest priority) ---
    if days_to_expiry <= 1:
        scores["EXPIRY"] += 10
        reasons.append(f"Expiry in {days_to_expiry} day(s)")

    # --- Rule 2: VIX based ---
    if vix > 25:
        scores["HIGH_VOL"] += 4
        reasons.append(f"VIX very high ({vix})")
    elif vix > 20:
        scores["HIGH_VOL"] += 2
        reasons.append(f"VIX elevated ({vix})")
    elif vix < 14:
        scores["SIDEWAYS"] += 2
        reasons.append(f"VIX low ({vix}) — calm market")

    # --- Rule 3: PCR based ---
    if pcr > 1.3:
        scores["TRENDING_UP"] += 3
        reasons.append(f"PCR very bullish ({pcr})")
    elif pcr > 1.1:
        scores["TRENDING_UP"] += 2
        reasons.append(f"PCR bullish ({pcr})")
    elif pcr < 0.7:
        scores["TRENDING_DOWN"] += 3
        reasons.append(f"PCR very bearish ({pcr})")
    elif pcr < 0.9:
        scores["TRENDING_DOWN"] += 2
        reasons.append(f"PCR bearish ({pcr})")
    else:
        scores["SIDEWAYS"] += 2
        reasons.append(f"PCR neutral ({pcr})")

    # --- Rule 4: Spot position in range ---
    range_size = resistance - support
    if range_size > 0:
        position = (nifty_spot - support) / range_size

        if position < 0.2:
            scores["TRENDING_DOWN"] += 2
            reasons.append(f"Spot near support ({support})")
        elif position > 0.8:
            scores["TRENDING_UP"] += 2
            reasons.append(f"Spot near resistance ({resistance})")
        else:
            scores["SIDEWAYS"] += 2
            reasons.append(f"Spot mid-range ({nifty_spot})")

    # --- Rule 5: IV based ---
    if avg_iv > 25:
        scores["HIGH_VOL"] += 2
        reasons.append(f"IV high ({avg_iv}%)")
    elif avg_iv < 12:
        scores["SIDEWAYS"] += 1
        reasons.append(f"IV low ({avg_iv}%)")

    # --- Pick regime with highest score ---
    regime = max(scores, key=scores.get)
    confidence = scores[regime]

    # Confidence label
    if confidence >= 8:
        conf_label = "Very High"
    elif confidence >= 5:
        conf_label = "High"
    elif confidence >= 3:
        conf_label = "Medium"
    else:
        conf_label = "Low"

    result = {
        "regime":          regime,
        "regime_label":    REGIMES[regime],
        "strategy":        REGIME_STRATEGY[regime],
        "emoji":           REGIME_EMOJI[regime],
        "confidence":      conf_label,
        "scores":          scores,
        "reasons":         reasons,
    }

    logger.info(f"🎯 Regime: {regime} ({conf_label}) | Strategy: {REGIME_STRATEGY[regime]}")
    return result
