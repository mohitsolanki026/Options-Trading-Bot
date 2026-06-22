import logging
from datetime import datetime
from utils.angel_helper import fetch_ltp
from utils.options_helper import (
    fetch_oi_data, summarise_options_chain,
    get_index_options, get_available_expiries_for_index
)
from utils.greeks_engine import analyse_atm_greeks
from utils.regime_detector import detect_regime
from utils.signal_engine import run_confluence
from utils.iv_history import record_iv, get_iv_rank, get_vix_rank
from utils.llm_brain import get_trade_decision
from utils.trade_journal import log_signal
from utils.telegram_helper import send_message, send_alert
from config.settings import INDICES, INDIA_VIX_TOKEN, INDIA_VIX_SYMBOL
from utils.technical import run_technical_analysis

logger = logging.getLogger(__name__)

def analyze_index(obj, df_scrip, index_key, vix_ltp, risk_status, position=None,
                  with_llm: bool = True):
    """
    Run the full analysis pipeline for one index and return the result.

    PURE w.r.t. Telegram — sends NO messages (except a failure alert), so it is
    safe to call every monitor cycle. Use ``scan_and_broadcast`` for the daily
    scan that should also push a summary to Telegram.

    with_llm : when False, the (slow, paid) LLM call is skipped — the code gate
    decides entries and hard rules manage exits. The monitor uses this on routine
    cycles; the LLM is invoked separately as an entry *veto* only when the gate is
    about to act (see monitor.handle_enter). Cuts ~150 LLM calls/day → a handful.
    """
    idx = INDICES[index_key]
    logger.info(f"\n{'='*40}")
    logger.info(f"📊 Scanning {index_key}...")

    try:
        # ── 1. Get expiry ──────────────────
        expiries = get_available_expiries_for_index(df_scrip, idx["scrip_name"])
        if not expiries:
            logger.warning(f"⚠️ No expiries found for {index_key}")
            return {}
        expiry = expiries[0]
        logger.info(f"📅 {index_key} expiry: {expiry}")

        # ── 2. Fetch spot ──────────────────
        spot_ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
        if not spot_ltp:
            logger.warning(f"⚠️ Could not fetch spot for {index_key}")
            return {}
        logger.info(f"💹 {index_key} spot: ₹{spot_ltp}")

        ta = run_technical_analysis(
            obj      = obj,
            token    = idx["token"],
            interval = "FIFTEEN_MINUTE",
            days_back = 5,
        )

        logger.info(f"📊 {index_key} TA: {ta.get('overall')} "
            f"Bull={ta.get('bull_count')} Bear={ta.get('bear_count')}")

        # ── 3. Options chain + OI ──────────
        options_df = get_index_options(df_scrip, idx["scrip_name"], expiry)
        if options_df.empty:
            logger.warning(f"⚠️ No options data for {index_key}")
            return {}

        strike_gap = idx.get("strike_gap", 50)
        df_oi   = fetch_oi_data(obj, options_df, spot_ltp, num_strikes=10,
                                strike_gap=strike_gap)
        summary = summarise_options_chain(df_oi, spot_ltp, strike_gap=strike_gap)

        # ── 4. Greeks ─────────────────────
        greeks  = analyse_atm_greeks(summary, expiry)
        logger.info(f"🧮 {index_key} Greeks: IV={greeks['avg_iv']}% Theta={greeks['theta']}")

        # ── 4b. IV rank (record today's IV, rank vs trailing history) ──
        avg_iv = float(greeks["avg_iv"] or 0.0)
        record_iv(index_key, avg_iv)
        iv_rank = get_iv_rank(index_key, avg_iv)
        if iv_rank is None:
            # Bootstrap: use India-VIX percentile until per-index history builds.
            iv_rank = get_vix_rank(obj, vix_ltp)
            if iv_rank is not None:
                logger.info(f"📈 {index_key} IV rank (VIX proxy): {iv_rank}")

        # ── 5. Regime ─────────────────────
        regime  = detect_regime(
            vix            = vix_ltp,
            pcr            = float(summary["pcr"] or 0),
            days_to_expiry = greeks["days_to_exp"],
            nifty_spot     = summary["nifty_spot"],
            support        = float(summary["support"] or 0),
            resistance     = float(summary["resistance"] or 0),
            avg_iv         = float(greeks["avg_iv"] or 0.0),
        )

        # ── 6. Confluence ──────────────────
        confluence = run_confluence(
            pcr            = float(summary["pcr"] or 0),
            support        = float(summary["support"] or 0),
            resistance     = float(summary["resistance"] or 0),
            nifty_spot     = summary["nifty_spot"],
            vix            = vix_ltp,
            days_to_expiry = greeks["days_to_exp"],
            regime         = regime["regime"],
            iv_rank        = iv_rank,
            ta             = ta,
        )

        # ── 7. LLM decision (skipped on routine cycles; used as entry veto only) ──
        if with_llm:
            decision = get_trade_decision(
                summary     = summary,
                greeks      = greeks,
                regime      = regime,
                confluence  = confluence,
                risk_status = risk_status,
                vix         = vix_ltp,
                position    = position,
                ta          = ta,
            )
        else:
            decision = {"action": "NONE", "confidence": "NONE",
                        "strategy": None, "reasoning": "LLM skipped (routine cycle)"}

        # ── 8. Log ────────────────────────
        log_signal(
            index_name = index_key,
            summary    = summary,
            greeks     = greeks,
            regime     = regime,
            confluence = confluence,
            decision   = decision,
            vix        = vix_ltp,
        )

        result = {
            "index":      index_key,
            "expiry":     expiry,
            "options_df": options_df,
            "df_oi":      df_oi,
            "spot_ltp":   spot_ltp,
            "summary":    summary,
            "greeks":     greeks,
            "regime":     regime,
            "confluence": confluence,
            "decision":   decision,
            "ta":         ta,
            "iv_rank":    iv_rank,
        }

        logger.info(f"✅ {index_key} → {decision.get('action')} ({regime['regime']})")
        return result

    except Exception as e:
        logger.error(f"❌ {index_key} scan failed at: {e}", exc_info=True)
        send_alert(f"❌ {index_key} Scan Failed", str(e), emoji="❌")
        return {}


def scan_and_broadcast(obj, df_scrip, index_key, vix_ltp, risk_status, position=None):
    """Analyse an index AND push a summary card to Telegram (daily 9:30 scan)."""
    result = analyze_index(obj, df_scrip, index_key, vix_ltp, risk_status, position)
    if result:
        _send_index_summary(
            index_key, result["spot_ltp"], result["summary"], result["greeks"],
            result["regime"], result["confluence"], result["decision"],
            result["expiry"], result["ta"],
        )
    return result

def _send_index_summary(index_key, spot_ltp, summary, greeks, regime, confluence, decision, expiry, ta):
    """Send compact multi-index summary to Telegram."""
    action_emoji = {
        "ENTER": "🟢", "HOLD": "🔵",
        "ADJUST": "🟡", "EXIT": "🔴", "SKIP": "⚪"
    }.get(decision.get("action"), "❓")

    ta_line = f"TA         : {ta.get('overall', 'N/A')} " \
            f"(🟢{ta.get('bull_count',0)} 🔴{ta.get('bear_count',0)})\n" \
            f"RSI        : {ta.get('rsi', 'N/A')}"
    msg = (
        f"{regime['emoji']} <b>{index_key}</b> | "
        f"{action_emoji} <b>{decision.get('action')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Spot       : ₹{spot_ltp}\n"
        f"Expiry     : {expiry} ({greeks['days_to_exp']}d)\n"
        f"Regime     : {regime['regime_label']}\n"
        f"Score      : {confluence['score']}/{confluence['max_score']}\n"
        f"{ta_line}\n"    
        f"PCR        : {summary['pcr']} | IV: {greeks['avg_iv']}%\n"
        f"Support    : {summary['support']} | Res: {summary['resistance']}\n"
        f"ATM CE/PE  : ₹{summary['atm_ce_ltp']} / ₹{summary['atm_pe_ltp']}\n"
        f"Theta      : ₹{greeks['theta']}/day\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 {decision.get('strategy')}\n"
        f"📝 {decision.get('reasoning', '')[:100]}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(msg)
