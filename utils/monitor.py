import time
import logging
from datetime import datetime, date
from utils.angel_helper import fetch_ltp
from utils.index_scanner import analyze_index
from utils.signal_engine import entry_allowed
from utils import strategies
from utils.telegram_helper import send_message, send_alert
from utils.websocket_feed import TICK_STORE
from config.settings import (
    INDICES, ACTIVE_INDICES,
    INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN,
)

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 300  # 5 minutes


# ─────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────

def is_market_open() -> bool:
    """True between 9:15 AM and 3:30 PM on weekdays."""
    now     = datetime.now()
    if now.weekday() >= 5:
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


def expiry_passed(expiry_str: str) -> bool:
    """True if the position's expiry date is today or earlier."""
    try:
        expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
        return date.today() >= expiry_date
    except Exception:
        return False


def force_exit_all_on_startup(STATE: dict):
    """
    On startup, force-exit any restored position whose expiry has already
    passed (so we never carry an expired/overnight position silently).
    """
    pt = STATE.get("paper_trader")
    if not pt:
        return
    for index_key in list(pt.open_trades.keys()):
        pos = pt.open_trades[index_key]
        if expiry_passed(pos.get("expiry", "")):
            logger.warning(f"⚠️ {index_key} position expired ({pos.get('expiry')}) — forcing exit.")
            send_alert("⚠️ Expired Position Exit",
                f"{index_key} {pos.get('strategy')} expired — closing on startup.",
                emoji="⚠️")
            df_oi = STATE.get("index_data", {}).get(index_key, {}).get("df_oi")
            handle_exit(STATE, index_key, "Expired position — startup forced exit", df_oi)


# ─────────────────────────────────────────
#  SINGLE MONITOR CYCLE
# ─────────────────────────────────────────

def run_monitor_cycle(STATE: dict):
    """One full 5-minute monitor cycle — analyses and manages every index."""
    obj = STATE.get("obj")
    if not obj or STATE.get("df_scrip") is None:
        logger.warning("⚠️ Monitor cycle skipped — obj or scrip master not ready.")
        return

    now_str = datetime.now().strftime("%H:%M")
    logger.info(f"🔄 Monitor cycle @ {now_str}")

    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp

    if not STATE.get("risk_manager"):
        from utils.risk_manager import RiskManager
        STATE["risk_manager"] = RiskManager()
    rm = STATE["risk_manager"]
    pt = STATE["paper_trader"]
    risk_status = rm.get_status()

    STATE.setdefault("index_data", {})

    for index_key in ACTIVE_INDICES:
        try:
            position = pt.get_position(index_key)
            result = analyze_index(
                obj, STATE["df_scrip"], index_key, vix_ltp, risk_status, position
            )
            if not result:
                continue

            STATE["index_data"][index_key] = result   # snapshot for tick monitor
            manage_index(STATE, index_key, result)

        except Exception as e:
            logger.error(f"❌ Monitor failed for {index_key}: {e}", exc_info=True)


def manage_index(STATE: dict, index_key: str, result: dict):
    """Manage a single index: stop-loss / target / EOD / LLM exit, or entry."""
    pt = STATE["paper_trader"]
    rm = STATE["risk_manager"]
    df_oi    = result.get("df_oi")
    decision = result.get("decision", {})
    now_str  = datetime.now().strftime("%H:%M")

    position = pt.get_position(index_key)

    if position:
        # ── 1. Stop loss / target (price-based) ──
        price_map = strategies.current_price_map(position, TICK_STORE, df_oi)
        level = pt.check_levels(index_key, price_map)
        if level == "STOP_LOSS":
            send_alert(f"🛑 {index_key} Stop Loss", "Premium against us.", emoji="🛑")
            handle_exit(STATE, index_key, "Stop loss triggered", df_oi)
            return
        if level == "TARGET":
            send_alert(f"🎯 {index_key} Target", "Target reached.", emoji="🎯")
            handle_exit(STATE, index_key, "Target achieved", df_oi)
            return

        # ── 2. End-of-day forced exit ──
        mins = minutes_to_close()
        if mins == 0:
            handle_exit(STATE, index_key, "EOD forced exit — market closed", df_oi)
            return
        if mins <= 30:
            send_alert(f"⏰ {index_key} EOD Exit",
                f"{mins} min to close — never carry overnight.", emoji="⏰")
            handle_exit(STATE, index_key, f"EOD forced exit — {mins}min to close", df_oi)
            return
        if mins <= 60:
            send_alert(f"⚠️ {index_key} 60 Min Warning",
                f"Will force-exit {position['strategy']} at the 30min mark.", emoji="⚠️")

        # ── 3. LLM-advised exit / adjust (advisory) ──
        action = decision.get("action", "HOLD")
        if action == "EXIT":
            handle_exit(STATE, index_key, decision.get("reasoning", "LLM exit"), df_oi)
        elif action == "ADJUST":
            handle_adjust(STATE, index_key, decision)
        else:
            logger.info(f"🔵 {index_key} HOLD — {decision.get('reasoning', '')[:70]}")
        return

    # ── 4. No position → CODE-GATED entry ──
    confluence  = result["confluence"]
    risk_status = rm.get_status()
    allowed, reason = entry_allowed(
        confluence      = confluence,
        risk_status     = risk_status,
        open_count      = pt.open_count(),
        max_positions   = rm.max_open_positions,
        in_entry_window = is_safe_to_enter(),
    )
    if not allowed:
        logger.info(f"⚪ {index_key} no entry @ {now_str}: {reason}")
        return

    handle_enter(STATE, index_key, result)


# ─────────────────────────────────────────
#  ACTION HANDLERS
# ─────────────────────────────────────────

def handle_enter(STATE: dict, index_key: str, result: dict):
    """Build and open a strategy-agnostic paper position for one index."""
    rm = STATE["risk_manager"]
    pt = STATE["paper_trader"]
    idx        = INDICES[index_key]
    lot_size   = idx["lot_size"]
    summary    = result["summary"]
    df_oi      = result["df_oi"]
    options_df = result["options_df"]
    confluence = result["confluence"]
    regime     = result["regime"]
    decision   = result["decision"]

    # Reference premium for rough sizing (real margin is applied in approve_trade)
    ref_price = float(summary.get("atm_ce_ltp", 0)) + float(summary.get("atm_pe_ltp", 0))
    if ref_price <= 0:
        logger.warning(f"⚠️ {index_key} entry skipped — no ATM premium available.")
        return

    approval = rm.approve_trade(pt.capital, ref_price, lot_size)
    if not approval["approved"]:
        logger.warning(f"🚫 {index_key} entry blocked: {approval['reason']}")
        send_alert(f"🚫 {index_key} Trade Blocked", approval["reason"], emoji="🚫")
        return

    lots     = approval["lots"]
    strategy = strategies.select_strategy(confluence, regime, decision)

    # Refine lots against the REAL margin Angel would require for these legs
    lots = rm.cap_lots_by_margin(
        obj=STATE.get("obj"), index=index_key, strategy=strategy,
        summary=summary, df_oi=df_oi, options_df=options_df,
        lot_size=lot_size, expiry=result["expiry"],
        capital=pt.capital, max_lots=lots,
    )
    if lots < 1:
        logger.warning(f"🚫 {index_key} entry blocked: margin exceeds allocation.")
        send_alert(f"🚫 {index_key} Trade Blocked",
            "Required margin exceeds capital allocation.", emoji="🚫")
        return

    position = strategies.build_position(
        index=index_key, strategy=strategy, summary=summary,
        df_oi=df_oi, options_df=options_df, lots=lots,
        lot_size=lot_size, expiry=result["expiry"],
    )
    if not position:
        send_alert(f"⚠️ {index_key} Build Failed",
            f"Could not price legs for {strategy}.", emoji="⚠️")
        return

    trade = pt.enter(position)
    if not trade:
        return

    # Subscribe all leg tokens for live tick monitoring
    ws = STATE.get("ws_feed")
    if ws:
        ws.subscribe("NFO", strategies.all_tokens(position))

    rm.add_position(
        symbol      = f"{index_key}{int(position['legs'][0]['strike'])}",
        entry_price = position["entry_combined"],
        lots        = lots,
        direction   = position["direction"],
    )

    legs_txt = "\n".join(
        f"  {l['action']} {l['option_type']} {int(l['strike'])} @ ₹{l['entry_ltp']}"
        for l in position["legs"]
    )
    send_message(
        f"🟢 <b>PAPER TRADE — ENTRY [{index_key}]</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy  : {strategy}\n"
        f"Lots      : {lots} × {lot_size}\n"
        f"Legs:\n{legs_txt}\n"
        f"Net Prem  : ₹{position['net_credit']} ({position['direction']})\n"
        f"Stop Loss : ₹{position['stop_loss_pnl']} P&L\n"
        f"Target    : ₹{position['target_pnl']} P&L\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning', '')[:160]}"
    )
    logger.info(f"🟢 {index_key} entered {strategy} lots={lots}")


def handle_exit(STATE: dict, index_key: str, reason: str, df_oi=None):
    """Close an index's open paper position."""
    pt = STATE["paper_trader"]
    rm = STATE["risk_manager"]

    position = pt.get_position(index_key)
    if not position:
        return

    price_map = strategies.current_price_map(position, TICK_STORE, df_oi)
    trade = pt.exit(index_key, price_map, reason=reason)
    if not trade:
        return

    total_pnl = trade["pnl"]
    rm.close_position(f"{index_key}{int(position['legs'][0]['strike'])}", total_pnl)

    emoji = "🟢" if total_pnl >= 0 else "🔴"
    send_message(
        f"{emoji} <b>PAPER TRADE — EXIT [{index_key}]</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy  : {trade['strategy']}\n"
        f"Entry     : ₹{trade['entry_combined']}\n"
        f"Exit      : ₹{trade['exit_combined']}\n"
        f"P&L       : ₹{total_pnl}\n"
        f"Day Total : ₹{rm.daily_pnl.total_pnl}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {reason}"
    )
    send_message(pt.get_stats_message())
    logger.info(f"🔴 {index_key} exit: P&L=₹{total_pnl}")


def handle_adjust(STATE: dict, index_key: str, decision: dict):
    pt  = STATE["paper_trader"]
    pos = pt.get_position(index_key)
    send_message(
        f"🟡 <b>ADJUST [{index_key}]</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Current : {pos.get('strategy')}\n"
        f"Action  : {decision.get('strategy')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {decision.get('reasoning')}"
    )
    logger.info(f"🟡 {index_key} adjust: {decision.get('strategy')}")


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
