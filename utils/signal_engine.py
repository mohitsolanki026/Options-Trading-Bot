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
import os

from utils import settings_store

logger = logging.getLogger(__name__)

# Fallbacks. The live values come from the settings store, so they can be
# retuned from the dashboard without a restart.
ENTRY_THRESHOLD = int(os.getenv("ENTRY_THRESHOLD", 4))
# Minimum weighted margin between opposing biases to call a direction "confirmed".
BIAS_MARGIN = 2
IV_RANK_SELL = int(os.getenv("IV_RANK_SELL", 55))
IV_RANK_BUY  = int(os.getenv("IV_RANK_BUY", 30))


def _tuned(key, fallback):
    """
    A dashboard override if the user set one, else this module's own constant.
    Keeps env configuration and tests working while still allowing live edits.
    """
    try:
        value = settings_store.override(key)
    except Exception:
        return fallback
    return fallback if value is None else value


# ─────────────────────────────────────────
#  SIGNALS  (each returns {weight-scored, label, value, bias})
#  bias ∈ {BULLISH, BEARISH, SELL_PREMIUM, BUY_OPTIONS, NEUTRAL}
# ─────────────────────────────────────────

def signal_pcr(pcr: float) -> dict:
    """Single PCR signal. Extremes weigh more; mid-range carries no edge → 0."""
    if pcr >= 1.5:
        return {"score": 2, "label": "PCR", "value": f"{pcr} — far more puts than calls, heavily oversold", "bias": "BULLISH"}
    if pcr >= 1.2:
        return {"score": 1, "label": "PCR", "value": f"{pcr} — more puts than calls, leaning up", "bias": "BULLISH"}
    if pcr <= 0.6:
        return {"score": 2, "label": "PCR", "value": f"{pcr} — far more calls than puts, heavily overbought", "bias": "BEARISH"}
    if pcr <= 0.8:
        return {"score": 1, "label": "PCR", "value": f"{pcr} — more calls than puts, leaning down", "bias": "BEARISH"}
    return {"score": 0, "label": "PCR", "value": f"{pcr} — evenly balanced, no edge", "bias": "NEUTRAL"}


def signal_oi_position(support: float, resistance: float, spot: float) -> dict:
    """
    OI structure. ONLY scores when spot is pressed against support/resistance
    (a real bounce/rejection edge). Mid-range = no edge → 0 (was wrongly +1).
    """
    rng = resistance - support
    if rng <= 0:
        return {"score": 0, "label": "OI", "value": "No clear range to work with", "bias": "NEUTRAL"}
    pos = (spot - support) / rng
    if pos < 0.2:
        return {"score": 1, "label": "OI", "value": f"Pressed against support at {int(support)}, a bounce is likely", "bias": "BULLISH"}
    if pos > 0.8:
        return {"score": 1, "label": "OI", "value": f"Pressed against resistance at {int(resistance)}, a pullback is likely", "bias": "BEARISH"}
    return {"score": 0, "label": "OI", "value": "Sitting mid-range, neither side pressed", "bias": "NEUTRAL"}


def signal_iv_rank(iv_rank, sell_at=None, buy_at=None) -> dict:
    """
    REAL IV rank (0-100) vs trailing history — the core edge for premium trades.
    Weighted 2. Unknown (insufficient history) → 0, no edge claimed.
    """
    sell_at = _tuned("iv_rank_sell", IV_RANK_SELL) if sell_at is None else sell_at
    buy_at  = _tuned("iv_rank_buy",  IV_RANK_BUY)  if buy_at  is None else buy_at
    if iv_rank is None:
        return {"score": 0, "label": "IV Rank", "value": "Not enough recorded history to judge", "bias": "NEUTRAL"}
    if iv_rank >= sell_at:
        return {"score": 2, "label": "IV Rank", "value": f"{iv_rank} out of 100 — expensive, worth selling", "bias": "SELL_PREMIUM"}
    if iv_rank <= buy_at:
        return {"score": 2, "label": "IV Rank", "value": f"{iv_rank} out of 100 — cheap, worth buying", "bias": "BUY_OPTIONS"}
    return {"score": 0, "label": "IV Rank", "value": f"{iv_rank} out of 100 — priced about normally", "bias": "NEUTRAL"}


def signal_vix(vix: float) -> dict:
    """VIX only carries edge at extremes; the calm/normal middle scores 0."""
    if vix is None:
        return {"score": 0, "label": "VIX", "value": "No reading available", "bias": "NEUTRAL"}
    if vix < 12:
        return {"score": 1, "label": "VIX", "value": f"India VIX {vix} — a very calm market", "bias": "SELL_PREMIUM"}
    if vix > 22:
        return {"score": 1, "label": "VIX", "value": f"India VIX {vix} — the market is fearful", "bias": "BUY_OPTIONS"}
    return {"score": 0, "label": "VIX", "value": f"India VIX {vix} — an ordinary reading", "bias": "NEUTRAL"}


def signal_theta_expiry(days_to_expiry: int) -> dict:
    """Near expiry, accelerating theta favours short premium."""
    if days_to_expiry is not None and days_to_expiry <= 2:
        return {"score": 1, "label": "Theta", "value": ("Expires today, so value drains fastest" if days_to_expiry == 0 else f"{days_to_expiry} day(s) to expiry, value drains fast"), "bias": "SELL_PREMIUM"}
    return {"score": 0, "label": "Theta", "value": f"{days_to_expiry} days to expiry, value drains slowly", "bias": "NEUTRAL"}


def signal_ta(ta: dict) -> dict:
    """Multi-indicator TA agreement (weighted 2). Needs 3+ indicators aligned."""
    if not ta or not ta.get("signals"):
        return {"score": 0, "label": "TA", "value": "No chart reading available", "bias": "NEUTRAL"}
    bull = ta.get("bull_count", 0)
    bear = ta.get("bear_count", 0)
    if bull >= 3:
        return {"score": 2, "label": "TA", "value": f"{bull} indicators point up against {bear} down", "bias": "BULLISH"}
    if bear >= 3:
        return {"score": 2, "label": "TA", "value": f"{bear} indicators point down against {bull} up", "bias": "BEARISH"}
    return {"score": 0, "label": "TA", "value": f"Mixed, {bull} up against {bear} down", "bias": "NEUTRAL"}


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

    threshold = _tuned("entry_threshold", ENTRY_THRESHOLD)
    sell_at   = _tuned("iv_rank_sell", IV_RANK_SELL)
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
    premium_sell_ok = (iv_rank is not None and iv_rank >= sell_at)

    decision = "✅ TAKE TRADE" if total_score >= threshold and bias_confirmed else "❌ SKIP"

    result = {
        "score":           total_score,
        "max_score":       MAX_SCORE,
        "threshold":       threshold,
        "iv_rank_sell":    sell_at,
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
#
# ``evaluate_entry`` runs EVERY check and reports all of them. The old version
# returned at the first failure, so it could only ever name one reason — fine
# for a log line, useless for a screen that has to explain a quiet day. The
# first failure, in the original order, is still surfaced as ``blocking`` so
# logs and alerts read exactly as before.

def _check(key, label, passed, detail, reason=None, na=False, help=""):
    return {"key": key, "label": label, "passed": bool(passed), "detail": detail,
            "reason": reason, "na": na, "help": help}


def evaluate_entry(
    confluence: dict,
    risk_status: dict,
    open_count: int,
    max_positions: int,
    in_entry_window: bool,
    blackout=(False, ""),
    in_cooldown=(False, ""),
    correlation=(True, ""),
) -> dict:
    """
    Authoritative GO/NO-GO for a new trade, enforced in code regardless of what
    the LLM thinks. Returns every check with a plain-English detail line:

        {"allowed": bool, "blocking": str|None, "checks": [ ... ]}

    Each check carries ``detail`` for people and ``reason`` for logs.
    """
    conf      = confluence or {}
    risk      = risk_status or {}
    score     = conf.get("score", 0)
    threshold = conf.get("threshold", _tuned("entry_threshold", ENTRY_THRESHOLD))
    max_score = conf.get("max_score", MAX_SCORE)
    bias      = conf.get("overall_bias", "NEUTRAL")
    margin    = conf.get("bias_margin", 0)
    iv_rank   = conf.get("iv_rank")
    sell_at   = conf.get("iv_rank_sell", _tuned("iv_rank_sell", IV_RANK_SELL))
    checks    = []

    # 1 ── enough weighted signals
    checks.append(_check(
        "score", "Enough signals agree",
        score >= threshold,
        f"Scored {score} out of {max_score}. Needs at least {threshold}.",
        reason=f"Score {score}/{max_score} < {threshold}",
        help="Six market checks are scored and weighted. Only the ones with a "
             "real edge score at all.",
    ))

    # 2 ── one view actually won
    bias_words = {
        "SELL_PREMIUM": "sell expensive options",
        "BUY_OPTIONS":  "buy cheap options",
        "BULLISH":      "the market goes up",
        "BEARISH":      "the market goes down",
        "NEUTRAL":      "no clear view",
    }
    confirmed = bool(conf.get("bias_confirmed", False))
    checks.append(_check(
        "bias", "One view clearly won",
        confirmed,
        (f"Leaning towards {bias_words.get(bias, bias.lower())}, ahead of the next "
         f"view by {margin} points. Needs {BIAS_MARGIN}.") if confirmed else
        (f"No view is ahead by enough. Best margin is {margin}, needs {BIAS_MARGIN}."),
        reason=f"Bias not confirmed (margin {margin})",
        help="A trade needs conviction, not just a passing score.",
    ))

    # 3 ── never sell cheap volatility
    selling = bias == "SELL_PREMIUM"
    premium_ok = bool(conf.get("premium_sell_ok", False))
    if not selling:
        checks.append(_check(
            "premium", "Options are pricey enough to sell", True,
            "Not a premium-selling trade, so this rule does not apply.",
            na=True,
            help="Only applies when the plan is to sell options.",
        ))
    else:
        checks.append(_check(
            "premium", "Options are pricey enough to sell",
            premium_ok,
            (f"Priced at {iv_rank} out of 100 against recent history. Needs {sell_at}."
             if iv_rank is not None else
             "Not enough recorded history yet to say whether options are expensive."),
            reason=f"IV rank too low to sell premium (IVR {iv_rank})",
            help="Selling options only pays when they are dearer than usual.",
        ))

    # 4 ── not halted
    halted = bool(risk.get("trading_halted"))
    checks.append(_check(
        "halted", "Trading is switched on",
        not halted,
        f"Halted: {risk.get('halt_reason')}" if halted else "Not paused.",
        reason=f"Trading halted: {risk.get('halt_reason')}",
        help="Either you paused it, or it stopped itself after a bad day.",
    ))

    # 5 ── daily loss budget
    headroom = risk.get("headroom", 1)
    checks.append(_check(
        "headroom", "Loss budget has room",
        headroom > 0,
        (f"₹{abs(round(headroom)):,} still available today." if headroom > 0
         else "Today's loss limit has been reached."),
        reason="Daily loss limit reached",
        help="Once the day's loss limit is hit, nothing new opens until tomorrow.",
    ))

    # 6 ── a free slot
    checks.append(_check(
        "slots", "A free slot",
        open_count < max_positions,
        f"{open_count} of {max_positions} slots in use.",
        reason=f"Max positions reached: {open_count}/{max_positions}",
        help="Caps how much of the account can be at risk at once.",
    ))

    # 7 ── inside the entry window
    checks.append(_check(
        "window", "Inside trading hours",
        bool(in_entry_window),
        "Within the window for opening new trades." if in_entry_window
        else "Outside the window for opening new trades.",
        reason="Outside safe entry window",
        help="Avoids the noisy open and leaves every trade time to work.",
    ))

    # 8 ── event / expiry blackout
    blocked_bo, bo_reason = blackout
    checks.append(_check(
        "blackout", "Not an expiry or event day",
        not blocked_bo,
        (bo_reason[:1].upper() + bo_reason[1:]) if blocked_bo
        else "No expiry or scheduled event today.",
        reason=f"Event/expiry blackout: {bo_reason}",
        help="Prices swing hardest on expiry days and around big announcements.",
    ))

    # 9 ── post-exit cooldown
    cooling, cd_reason = in_cooldown
    checks.append(_check(
        "cooldown", "Cooling-off period is over",
        not cooling,
        cd_reason if cooling else "Nothing closed here recently.",
        reason=f"Post-exit cooldown: {cd_reason}",
        help="Stops the bot jumping straight back into a trade it just left.",
    ))

    # 10 ── correlated exposure
    corr_ok, corr_reason = correlation
    checks.append(_check(
        "correlation", "Not the same bet twice",
        corr_ok,
        corr_reason if not corr_ok else "No overlapping position elsewhere.",
        reason=f"Correlated exposure: {corr_reason}",
        help="NIFTY, BANKNIFTY and FINNIFTY move together, so two sold-premium "
             "trades across them double the risk rather than spreading it.",
    ))

    failed   = [c for c in checks if not c["passed"]]
    blocking = failed[0]["reason"] if failed else None
    allowed  = not failed

    logger.info(
        f"🚦 Gate: {'ALLOW' if allowed else 'BLOCK'} "
        f"({len(checks) - len(failed)}/{len(checks)} checks passed)"
        + (f" — {blocking}" if blocking else "")
    )
    return {
        "allowed":  allowed,
        "blocking": blocking,
        "failed":   [c["key"] for c in failed],
        "passed":   len(checks) - len(failed),
        "total":    len(checks),
        "checks":   checks,
    }


def entry_allowed(*args, **kwargs) -> tuple[bool, str]:
    """Backwards-compatible ``(allowed, reason)`` view of ``evaluate_entry``."""
    verdict = evaluate_entry(*args, **kwargs)
    return verdict["allowed"], (verdict["blocking"] or "OK")
