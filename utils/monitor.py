import time
import logging
from datetime import datetime
from utils.angel_helper import fetch_ltp
from utils.options_helper import fetch_oi_data, summarise_options_chain
from utils.greeks_engine import analyse_atm_greeks
from utils.regime_detector import detect_regime
from utils.signal_engine import run_confluence
from utils.llm_brain import get_trade_decision
from utils.trade_journal import log_signal
from utils.telegram_helper import send_message, send_alert
from config.settings import INDICES, ACTIVE_INDEX

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 300  # 5 minutes


# ─────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────

def is_market_open() -> bool:
    """True between 9:15 AM and 3:30 PM on weekdays."""
    now     = datetime.now()
    weekday = now.weekday()
    if weekday >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)


def is_market_hours() -> bool:
    """True between 9:30 AM and 3:30 PM — safe trading window."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t <= (15 * 60 + 30)


def is_safe_to_enter() -> bool:
    """No new entries before 9:40 AM or after 2:00 PM."""
    now = datetime.now()
    t   = now.hour * 60 + now.minute
    return (9 * 60 + 40) <= t <= (14 * 60 + 0)


def minutes_to_close() -> int:
    """Minutes until 3:00 PM market close."""
    now   = datetime.now()
    close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now >= close:
        return 0
    return int((close - now).total_seconds() / 60)


def should_force_exit_on_startup(STATE: dict) -> bool:
    """
    On startup, if we have an open position from yesterday
    or from a session that ran past market close, force exit.
    """
    pos = STATE.get("current_position")
    if not pos:
        return False

    # If market is currently closed and we have a position → force exit
    if not is_market_hours():
        logger.warning("⚠️ Open position found but market is closed — will exit at next open.")
        return False  # Don't exit in after-hours, wait for market open

    # If expiry date has passed → force exit
    from datetime import date
    expiry_str = pos.get("expiry", "")
    try:
        expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
        if date.today() > expiry_date:
            logger.warning(f"⚠️ Position expired: {expiry_str} — forcing exit.")
            return True
    except Exception:
        pass

    return False


# ─────────────────────────────────────────
#  SINGLE MONITOR CYCLE
# ─────────────────────────────────────────

def run_monitor_cycle(STATE: dict):
    """One full 5-minute monitor cycle."""
    obj        = STATE.get("obj")
    options_df = STATE.get("options_df")
    expiry     = STATE.get("expiry")
    now_str    = datetime.now().strftime("%H:%M")

    if not obj or options_df is None:
        logger.warning("⚠️ Monitor cycle skipped — obj or options_df not ready.")
        return

    logger.info(f"🔄 Monitor cycle @ {now_str}")

    # ── 1. Fetch live prices ──────────────
    idx       = INDICES[ACTIVE_INDEX]
    nifty_ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    if not nifty_ltp:
        logger.warning("⚠️ Could not fetch LTP — skipping cycle.")
        return

    STATE["nifty_ltp"] = nifty_ltp
    STATE["vix_ltp"]   = vix_ltp

    # ── 2. Recalculate signals ────────────
    try:
        df_oi   = fetch_oi_data(obj, options_df, nifty_ltp, num_strikes=10)
        summary = summarise_options_chain(df_oi, nifty_ltp)
        greeks  = analyse_atm_greeks(summary, expiry)

        regime = detect_regime(
            vix            = vix_ltp,
            pcr            = float(summary["pcr"] or 0),
            days_to_expiry = greeks["days_to_exp"],
            nifty_spot     = summary["nifty_spot"],
            support        = float(summary["support"] or 0),
            resistance     = float(summary["resistance"] or 0),
            avg_iv         = float(greeks["avg_iv"] or 0),
        )

        confluence = run_confluence(
            pcr            = float(summary["pcr"] or 0),
            sentiment      = summary["sentiment"],
            support        = float(summary["support"] or 0),
            resistance     = float(summary["resistance"] or 0),
            nifty_spot     = summary["nifty_spot"],
            avg_iv         = float(greeks["avg_iv"] or 0),
            vix            = vix_ltp,
            days_to_expiry = greeks["days_to_exp"],
            theta          = float(greeks["theta"] or 0),
            regime         = regime["regime"],
        )

        STATE["summary"]    = summary
        STATE["greeks"]     = greeks
        STATE["regime"]     = regime
        STATE["confluence"] = confluence

    except Exception as e:
        logger.error(f"❌ Signal recalc failed: {e}")
        return

    # ── 3. Risk manager ───────────────────
    if not STATE.get("risk_manager"):
        from utils.risk_manager import RiskManager
        STATE["risk_manager"] = RiskManager()
    risk_status = STATE["risk_manager"].get_status()

    # ── 4. Check stop loss FIRST ──────────
    if STATE.get("current_position"):
        check_stop_loss(STATE, STATE["current_position"], nifty_ltp)

    # ── 5. EOD forced exit ────────────────
    if STATE.get("current_position"):
        mins = minutes_to_close()
        pos  = STATE["current_position"]
        if mins == 0:
            logger.warning("⏰ Market closed — forcing exit NOW.")
            send_alert("⏰ EOD Forced Exit",
                f"Market closed. Closing {pos['strategy']}.", emoji="⏰")
            handle_exit(STATE, {"reasoning": "EOD forced exit — market closed"}, nifty_ltp)
        elif mins <= 30:
            logger.warning(f"⏰ {mins} min to close — forcing exit.")
            send_alert("⏰ Forced EOD Exit",
                f"{mins} min to close. Closing {pos['strategy']}.\n"
                f"Never carry options overnight.", emoji="⏰")
            handle_exit(STATE, {"reasoning": f"EOD forced exit — {mins}min to close"}, nifty_ltp)
        elif mins <= 60:
            send_alert("⚠️ 60 Min Warning",
                f"Position open: {pos['strategy']}\n"
                f"Will force-exit at 30min mark.", emoji="⚠️")

    # ── 6. Expiry day forced exit ─────────
    # if STATE.get("current_position") and greeks.get("days_to_exp", 1) == 0:
    #     logger.warning("⏰ EXPIRY DAY — closing open position.")
    #     send_alert("⏰ Expiry Day Exit",
    #         "Closing position — expiry day, no carry forward.", emoji="⏰")
    #     handle_exit(STATE, {"reasoning": "Expiry day forced exit"}, nifty_ltp)

    # ── 7. LLM decision ───────────────────
    if not STATE.get("current_position"):
        # Only ask LLM for entry if no position open
        decision = get_trade_decision(
            summary     = summary,
            greeks      = greeks,
            regime      = regime,
            confluence  = confluence,
            risk_status = risk_status,
            vix         = vix_ltp,
            position    = None,
        )
        STATE["decision"] = decision

        log_signal(
            index_name = ACTIVE_INDEX,
            summary    = summary,
            greeks     = greeks,
            regime     = regime,
            confluence = confluence,
            decision   = decision,
            vix        = vix_ltp,
        )

        action = decision.get("action", "SKIP")
        if action == "ENTER" and is_safe_to_enter():
            handle_enter(STATE, decision, summary, greeks)
        else:
            logger.info(f"⚪ {action} @ {now_str} | Score={confluence['score']}/7")

    else:
        # Position is open — ask LLM HOLD/ADJUST/EXIT
        decision = get_trade_decision(
            summary     = summary,
            greeks      = greeks,
            regime      = regime,
            confluence  = confluence,
            risk_status = risk_status,
            vix         = vix_ltp,
            position    = STATE.get("current_position"),
        )
        STATE["decision"] = decision
        action = decision.get("action", "HOLD")

        if action == "EXIT":
            handle_exit(STATE, decision, nifty_ltp)
        elif action == "ADJUST":
            handle_adjust(STATE, decision)
        else:
            logger.info(f"🔵 {action} — {decision.get('reasoning', '')[:80]}")


# ─────────────────────────────────────────
#  ACTION HANDLERS
# ─────────────────────────────────────────

def handle_enter(STATE: dict, decision: dict, summary: dict, greeks: dict):
    """Paper trade entry."""
    rm      = STATE["risk_manager"]
    capital = STATE.get("capital", 100000)
    ce_ltp  = float(summary["atm_ce_ltp"])
    pe_ltp  = float(summary["atm_pe_ltp"])
    strike  = summary["atm_strike"]

    approval = rm.approve_trade(capital, ce_ltp)
    if not approval["approved"]:
        logger.warning(f"🚫 Entry blocked: {approval['reason']}")
        send_alert("🚫 Trade Blocked", approval["reason"], emoji="🚫")
        return

    lots = approval["lots"]
    pt   = STATE["paper_trader"]
    trade = pt.enter(
        index    = ACTIVE_INDEX,
        strategy = decision.get("strategy", "Short Straddle"),
        strike   = strike,
        ce_ltp   = ce_ltp,
        pe_ltp   = pe_ltp,
        lots     = lots,
        lot_size = INDICES[ACTIVE_INDEX]["lot_size"],
        expiry   = STATE["expiry"],
    )
    STATE["current_position"] = trade

    rm.add_position(
        symbol      = f"NIFTY{strike}",
        entry_price = ce_ltp + pe_ltp,
        lots        = lots,
        direction   = "SELL",
    )

    msg = (
        f"🟢 <b>PAPER TRADE — ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy  : {decision.get('strategy')}\n"
        f"Strike    : {strike}\n"
        f"CE Sell   : ₹{ce_ltp}\n"
        f"PE Sell   : ₹{pe_ltp}\n"
        f"Combined  : ₹{ce_ltp + pe_ltp}\n"
        f"Lots      : {lots}\n"
        f"Stop Loss : ₹{round((ce_ltp + pe_ltp) * 1.4, 2)}\n"
        f"Target    : ₹{round((ce_ltp + pe_ltp) * 0.5, 2)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning')}"
    )
    send_message(msg)
    logger.info(f"🟢 Entered: {strike} CE={ce_ltp} PE={pe_ltp}")


def handle_exit(STATE: dict, decision: dict, current_ltp: float):
    """Paper trade exit."""
    pos = STATE.get("current_position")
    if not pos:
        return

    rm  = STATE["risk_manager"]
    pt  = STATE["paper_trader"]

    # Use tick store prices if available, else fallback to summary
    from utils.websocket_feed import TICK_STORE
    ce_token = str(STATE.get("ce_token", ""))
    pe_token = str(STATE.get("pe_token", ""))

    ce_ltp = TICK_STORE.get_ltp(ce_token) if ce_token else 0
    pe_ltp = TICK_STORE.get_ltp(pe_token) if pe_token else 0

    # Fallback to summary if tick store empty
    if ce_ltp == 0 or pe_ltp == 0:
        summary = STATE.get("summary", {})
        ce_ltp  = float(summary.get("atm_ce_ltp", 0))
        pe_ltp  = float(summary.get("atm_pe_ltp", 0))

    trade = pt.exit(ce_ltp, pe_ltp, reason=decision.get("reasoning", "Exit"))
    if not trade:
        return

    total_pnl     = trade["pnl"]
    combined_exit = trade["exit_premium"]

    rm.close_position(f"NIFTY{pos['strike']}", combined_exit)
    STATE["current_position"] = None

    emoji = "🟢" if total_pnl >= 0 else "🔴"
    msg = (
        f"{emoji} <b>PAPER TRADE — EXIT</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy  : {pos['strategy']}\n"
        f"Strike    : {pos['strike']}\n"
        f"Entry     : ₹{pos['combined_premium']}\n"
        f"Exit      : ₹{combined_exit}\n"
        f"P&L       : ₹{total_pnl}\n"
        f"Day Total : ₹{rm.daily_pnl.total_pnl}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning', 'Exit triggered')}"
    )
    send_message(msg)
    send_message(pt.get_stats_message())
    logger.info(f"🔴 Exit: P&L=₹{total_pnl}")


def handle_adjust(STATE: dict, decision: dict):
    pos = STATE.get("current_position")
    msg = (
        f"🟡 <b>ADJUST POSITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Current : {pos.get('strategy')}\n"
        f"Action  : {decision.get('strategy')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning')}"
    )
    send_message(msg)
    logger.info(f"🟡 Adjust: {decision.get('strategy')}")


def check_stop_loss(STATE: dict, pos: dict, nifty_ltp: float):
    """Check stop loss and target using best available price."""
    from utils.websocket_feed import TICK_STORE

    ce_token = str(STATE.get("ce_token", ""))
    pe_token = str(STATE.get("pe_token", ""))

    ce_ltp = TICK_STORE.get_ltp(ce_token) if ce_token else 0
    pe_ltp = TICK_STORE.get_ltp(pe_token) if pe_token else 0

    if ce_ltp == 0 or pe_ltp == 0:
        summary = STATE.get("summary", {})
        ce_ltp  = float(summary.get("atm_ce_ltp", 0))
        pe_ltp  = float(summary.get("atm_pe_ltp", 0))

    if ce_ltp == 0 or pe_ltp == 0:
        return

    current = ce_ltp + pe_ltp
    sl      = pos.get("stop_loss", float("inf"))
    target  = pos.get("target", 0)

    if current >= sl:
        logger.warning(f"🛑 SL HIT: {current:.2f} >= {sl}")
        send_alert("🛑 Stop Loss Hit",
            f"Combined ₹{current:.2f} >= SL ₹{sl}", emoji="🛑")
        handle_exit(STATE, {"reasoning": "Stop loss triggered"}, nifty_ltp)
    elif current <= target:
        logger.info(f"🎯 TARGET HIT: {current:.2f} <= {target}")
        send_alert("🎯 Target Hit",
            f"Combined ₹{current:.2f} <= Target ₹{target}", emoji="🎯")
        handle_exit(STATE, {"reasoning": "Target achieved"}, nifty_ltp)


# ─────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────

def start_monitor(STATE: dict):
    """Runs every 5 minutes. Handles market hours correctly."""
    logger.info("👁️ Monitor loop started.")

    while True:
        try:
            if is_market_hours():
                run_monitor_cycle(STATE)
            else:
                now = datetime.now().strftime("%H:%M")
                logger.info(f"💤 Market closed @ {now} — monitor sleeping.")

        except Exception as e:
            logger.error(f"❌ Monitor cycle error: {e}", exc_info=True)
            try:
                send_alert("❌ Monitor Error", str(e), emoji="❌")
            except Exception:
                pass

        time.sleep(MONITOR_INTERVAL)