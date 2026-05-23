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
from utils.llm_brain import get_trade_decision
from utils.trade_journal import log_signal
from utils.telegram_helper import send_message, send_alert
from config.settings import INDICES, INDIA_VIX_TOKEN, INDIA_VIX_SYMBOL

logger = logging.getLogger(__name__)

def scan_index(obj, df_scrip, index_key, vix_ltp, risk_manager, paper_trader, current_positions):
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

        # ── 3. Options chain + OI ──────────
        options_df = get_index_options(df_scrip, idx["scrip_name"], expiry)
        if options_df.empty:
            logger.warning(f"⚠️ No options data for {index_key}")
            return {}

        df_oi   = fetch_oi_data(obj, options_df, spot_ltp, num_strikes=10)
        summary = summarise_options_chain(df_oi, spot_ltp)

        # ── 4. Greeks ─────────────────────
        greeks  = analyse_atm_greeks(summary, expiry)
        logger.info(f"🧮 {index_key} Greeks: IV={greeks['avg_iv']}% Theta={greeks['theta']}")

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
            sentiment      = summary["sentiment"],
            support        = float(summary["support"] or 0),
            resistance     = float(summary["resistance"] or 0),
            nifty_spot     = summary["nifty_spot"],
            avg_iv         = float(greeks["avg_iv"] or 0.0),
            vix            = vix_ltp,
            days_to_expiry = greeks["days_to_exp"],
            theta          = float(greeks["theta"] or 0.0),
            regime         = regime["regime"],
        )

        # ── 7. LLM decision ───────────────
        risk_status = risk_manager.get_status()
        position    = current_positions.get(index_key)
        decision    = get_trade_decision(
            summary     = summary,
            greeks      = greeks,
            regime      = regime,
            confluence  = confluence,
            risk_status = risk_status,
            vix         = vix_ltp,
            position    = position,
        )

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

        # ── 9. Telegram ───────────────────
        _send_index_summary(index_key, spot_ltp, summary,
                            greeks, regime, confluence, decision, expiry)

        result = {
            "index":      index_key,
            "expiry":     expiry,
            "options_df": options_df,
            "spot_ltp":   spot_ltp,
            "summary":    summary,
            "greeks":     greeks,
            "regime":     regime,
            "confluence": confluence,
            "decision":   decision,
        }

        logger.info(f"✅ {index_key} → {decision.get('action')} ({regime['regime']})")
        return result

    except Exception as e:
        logger.error(f"❌ {index_key} scan failed at: {e}", exc_info=True)
        send_alert(f"❌ {index_key} Scan Failed", str(e), emoji="❌")
        return {}

def _send_index_summary(index_key, spot_ltp, summary, greeks, regime, confluence, decision, expiry):
    """Send compact multi-index summary to Telegram."""
    action_emoji = {
        "ENTER": "🟢", "HOLD": "🔵",
        "ADJUST": "🟡", "EXIT": "🔴", "SKIP": "⚪"
    }.get(decision.get("action"), "❓")

    msg = (
        f"{regime['emoji']} <b>{index_key}</b> | "
        f"{action_emoji} <b>{decision.get('action')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Spot       : ₹{spot_ltp}\n"
        f"Expiry     : {expiry} ({greeks['days_to_exp']}d)\n"
        f"Regime     : {regime['regime_label']}\n"
        f"Score      : {confluence['score']}/{confluence['max_score']}\n"
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
