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
from utils.telegram_helper import send_message
from config.settings import INDICES, ACTIVE_INDEX

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 300  # 5 minutes in seconds


# ─────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────

def is_market_open() -> bool:
    """True between 9:30 AM and 2:30 PM on weekdays."""
    now     = datetime.now()
    weekday = now.weekday()
    if weekday >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t <= (14 * 60 + 30)


def is_safe_to_enter() -> bool:
    """Avoid first 15 min and last 60 min of session."""
    now = datetime.now()
    t   = now.hour * 60 + now.minute
    # No entry before 9:40 AM or after 2:00 PM
    return (9 * 60 + 40) <= t <= (14 * 60 + 0)


def minutes_to_close() -> int:
    """How many minutes until 3:00 PM."""
    now     = datetime.now()
    close   = now.replace(hour=15, minute=0, second=0)
    delta   = (close - now).total_seconds() / 60
    return max(0, int(delta))


# ─────────────────────────────────────────
#  SINGLE MONITOR CYCLE
# ─────────────────────────────────────────

def run_monitor_cycle(STATE: dict):
    """
    One full monitor cycle:
    1. Fetch live data
    2. Recalculate signals
    3. Ask LLM → HOLD / ADJUST / EXIT
    4. Act on decision
    5. Log everything
    """
    obj        = STATE["obj"]
    options_df = STATE["options_df"]
    expiry     = STATE["expiry"]
    now        = datetime.now().strftime("%H:%M")

    logger.info(f"🔄 Monitor cycle @ {now}")

    # ── 1. Fetch live prices ──────────────
    idx       = INDICES[ACTIVE_INDEX]
    nifty_ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    if not nifty_ltp:
        logger.warning("⚠️ Could not fetch Nifty LTP — skipping cycle.")
        return

    STATE["nifty_ltp"] = nifty_ltp
    STATE["vix_ltp"]   = vix_ltp

    # ── 2. Fetch OI + recalculate signals ─
    df_oi   = fetch_oi_data(obj, options_df, nifty_ltp, num_strikes=10)
    summary = summarise_options_chain(df_oi, nifty_ltp)
    greeks  = analyse_atm_greeks(summary, expiry)

    regime = detect_regime(
        vix            = vix_ltp,
        pcr            = float(summary["pcr"]),
        days_to_expiry = greeks["days_to_exp"],
        nifty_spot     = summary["nifty_spot"],
        support        = float(summary["support"]),
        resistance     = float(summary["resistance"]),
        avg_iv         = float(greeks["avg_iv"]),
    )

    confluence = run_confluence(
        pcr            = float(summary["pcr"]),
        sentiment      = summary["sentiment"],
        support        = float(summary["support"]),
        resistance     = float(summary["resistance"]),
        nifty_spot     = summary["nifty_spot"],
        avg_iv         = float(greeks["avg_iv"]),
        vix            = vix_ltp,
        days_to_expiry = greeks["days_to_exp"],
        theta          = float(greeks["theta"]),
        regime         = regime["regime"],
    )

    STATE["summary"]    = summary
    STATE["greeks"]     = greeks
    STATE["regime"]     = regime
    STATE["confluence"] = confluence

    # ── 3. Ask LLM ────────────────────────
    if not STATE.get("risk_manager"):
        from utils.risk_manager import RiskManager
        STATE["risk_manager"] = RiskManager()

    risk_status = STATE["risk_manager"].get_status()

    # If position is open → ask HOLD/ADJUST/EXIT
    # If no position → ask ENTER/SKIP
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

    # ── 4. Log to journal ─────────────────
    log_signal(
        index_name = "NIFTY",
        summary    = summary,
        greeks     = greeks,
        regime     = regime,
        confluence = confluence,
        decision   = decision,
        vix        = vix_ltp,
    )

    # ── 5. Act on decision ────────────────
    action = decision.get("action", "SKIP")

    if action == "ENTER" and is_safe_to_enter():
        handle_enter(STATE, decision, summary, greeks)

    elif action == "EXIT" and STATE.get("current_position"):
        handle_exit(STATE, decision, nifty_ltp)

    elif action == "ADJUST" and STATE.get("current_position"):
        handle_adjust(STATE, decision)

    elif action == "HOLD":
        logger.info(f"🔵 HOLD — {decision.get('reasoning', '')[:80]}")

    else:
        logger.info(f"⚪ SKIP @ {now} | Score={confluence['score']}/6")

    # ── 6. Check stop losses first ──────────
    if STATE.get("current_position"):
        check_stop_loss(STATE, STATE["current_position"], nifty_ltp)

    # ── 7. Force exit near close ─────────────
    if STATE.get("current_position"):
        pos  = STATE["current_position"]
        mins = minutes_to_close()
        if mins <= 30:
            handle_exit(STATE, {"reasoning": f"EOD forced exit"}, nifty_ltp)
        elif mins <= 60:
            send_alert("⚠️ 60 Min Warning", f"Position open: {pos['strategy']}", emoji="⚠️")




# ─────────────────────────────────────────
#  ACTION HANDLERS
# ─────────────────────────────────────────

def handle_enter(STATE: dict, decision: dict, summary: dict, greeks: dict):
    """Paper trade entry — logs position to STATE."""
    rm       = STATE["risk_manager"]
    capital  = STATE.get("capital", 100000)
    ce_ltp   = float(summary["atm_ce_ltp"])
    pe_ltp   = float(summary["atm_pe_ltp"])
    strike   = summary["atm_strike"]
    expiry   = STATE["expiry"]

    # Risk approval
    approval = rm.approve_trade(capital, ce_ltp)
    if not approval["approved"]:
        logger.warning(f"🚫 Entry blocked by Risk Manager: {approval['reason']}")
        send_alert("🚫 Trade Blocked", approval["reason"], emoji="🚫")
        return

    lots = approval["lots"]

    # Record position in STATE (paper trade — no real order yet)
    pt    = STATE["paper_trader"]
    trade = pt.enter(
        index     = "NIFTY",
        strategy  = decision.get("strategy", "Short Straddle"),
        strike    = summary["atm_strike"],
        ce_ltp    = ce_ltp,
        pe_ltp    = pe_ltp,
        lots      = lots,
        lot_size  = INDICES[ACTIVE_INDEX]["lot_size"],
        expiry    = STATE["expiry"],
    )
    STATE["current_position"] = trade
    symbol = f"NIFTY{strike}"
  
    rm.add_position(
        symbol      = symbol,
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
    logger.info(f"🟢 Paper trade entered: {strike} CE={ce_ltp} PE={pe_ltp}")


def handle_exit(STATE, decision, current_ltp):
    pos = STATE.get("current_position")
    if not pos:
        return

    rm     = STATE["risk_manager"]
    pt     = STATE["paper_trader"]
    ce_ltp = float(STATE["summary"]["atm_ce_ltp"])
    pe_ltp = float(STATE["summary"]["atm_pe_ltp"])

    # Single exit — let paper_trader calculate P&L
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
    from utils.telegram_helper import send_message
    send_message(msg)
    send_message(pt.get_stats_message())
    logger.info(f"🔴 Paper trade exited: P&L=₹{total_pnl}")

def handle_adjust(STATE: dict, decision: dict):
    """Notify about position adjustment needed."""
    pos = STATE.get("current_position")
    msg = (
        f"🟡 <b>ADJUST POSITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Current   : {pos['symbol']}\n"
        f"Action    : {decision.get('strategy')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning')}"
    )
    send_message(msg)
    logger.info(f"🟡 Adjust signal: {decision.get('strategy')}")


def check_stop_loss(STATE: dict, pos: dict, nifty_ltp: float):
    """Check if stop loss or target has been hit."""
    ce_ltp  = float(STATE["summary"]["atm_ce_ltp"])
    pe_ltp  = float(STATE["summary"]["atm_pe_ltp"])
    current = ce_ltp + pe_ltp

    if current >= pos["stop_loss"]:
        logger.warning(f"🛑 STOP LOSS HIT: current={current} >= sl={pos['stop_loss']}")
        send_alert("🛑 Stop Loss Hit", f"Combined premium ₹{current} hit stop ₹{pos['stop_loss']}")
        handle_exit(STATE, {"reasoning": "Stop loss triggered"}, nifty_ltp)

    elif current <= pos["target"]:
        logger.info(f"🎯 TARGET HIT: current={current} <= target={pos['target']}")
        send_alert("🎯 Target Hit", f"Combined premium ₹{current} hit target ₹{pos['target']}")
        handle_exit(STATE, {"reasoning": "Target achieved"}, nifty_ltp)


# ─────────────────────────────────────────
#  MAIN MONITORING LOOP
# ─────────────────────────────────────────

def start_monitor(STATE: dict):
    """
    Runs every 5 minutes during market hours.
    Call this from bot.py in a separate thread.
    """
    logger.info("👁️ Monitor loop started.")

    while True:
        try:
            if is_market_open():
                run_monitor_cycle(STATE)
            else:
                now = datetime.now().strftime("%H:%M")
                logger.info(f"💤 Market closed @ {now} — monitor sleeping.")

        except Exception as e:
            logger.error(f"❌ Monitor cycle error: {e}")
            send_alert("❌ Monitor Error", str(e), emoji="❌")

        time.sleep(MONITOR_INTERVAL)