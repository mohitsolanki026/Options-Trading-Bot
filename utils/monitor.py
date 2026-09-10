import logging
import threading
import time
from datetime import datetime, date
from utils.angel_helper import fetch_ltp
from utils.index_scanner import analyze_index
from utils.signal_engine import evaluate_entry
from utils.event_calendar import is_blackout
from utils import events, settings_store, strategies
from utils.runtime import RUNTIME
from utils.trade_journal import log_gate
from utils.websocket_feed import TICK_STORE
from config.settings import (
    INDICES, ACTIVE_INDICES,
    INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN,
    ENTRY_COOLDOWN_MIN,
)

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 300  # 5 minutes

# Set to cut the wait short. The dashboard's "check now" uses this instead of
# spawning a second cycle, so two scans can never overlap.
_wake = threading.Event()


def request_rescan():
    """Ask the monitor loop to run its next cycle immediately."""
    _wake.set()


def active_indices() -> list:
    """Indices to scan right now — editable from the dashboard, env is the default."""
    try:
        chosen = settings_store.override("active_indices")
    except Exception:
        chosen = None
    picked = ACTIVE_INDICES if chosen is None else chosen
    return [i for i in picked if i in INDICES] or list(ACTIVE_INDICES)


def _cooldown_minutes() -> int:
    try:
        chosen = settings_store.override("entry_cooldown_min")
    except Exception:
        chosen = None
    return ENTRY_COOLDOWN_MIN if chosen is None else chosen


def _window() -> tuple:
    """(start, end) of the new-entry window as minutes past midnight."""
    def mins(value, fallback):
        try:
            h, m = str(value).split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return fallback
    try:
        start = settings_store.get("entry_window_start")
        end   = settings_store.get("entry_window_end")
    except Exception:
        start, end = "09:40", "14:00"
    return mins(start, 9 * 60 + 40), mins(end, 14 * 60)


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
    """
    True inside the configured new-entry window. Defaults to 9:40 am - 2:00 pm,
    which skips the noisy open and leaves every trade time to work.
    """
    now = datetime.now()
    t   = now.hour * 60 + now.minute
    start, end = _window()
    return start <= t <= end


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
            events.warning("position.expired", "Expired position closed",
                f"The {pos.get('strategy')} on {index_key} had already expired, "
                f"so it was closed as soon as the bot restarted.", index=index_key)
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

    cycle_started = time.time()
    now_str = datetime.now().strftime("%H:%M")
    logger.info(f"🔄 Monitor cycle @ {now_str}")

    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp
    RUNTIME.series.record("vix", vix_ltp)

    if not STATE.get("risk_manager"):
        from utils.risk_manager import RiskManager
        STATE["risk_manager"] = RiskManager()
    rm = STATE["risk_manager"]
    rm.refresh()                       # pick up anything changed in the dashboard
    pt = STATE["paper_trader"]
    risk_status = rm.get_status()

    STATE.setdefault("index_data", {})
    indices = active_indices()
    STATE["active_indices"] = indices

    for index_key in indices:
        try:
            position = pt.get_position(index_key)
            # Routine cycle → no LLM (the gate decides; LLM is an entry veto only).
            result = analyze_index(
                obj, STATE["df_scrip"], index_key, vix_ltp, risk_status, position,
                with_llm=False,
            )
            if not result:
                continue

            STATE["index_data"][index_key] = result   # snapshot for tick monitor
            manage_index(STATE, index_key, result)

        except Exception as e:
            logger.error(f"❌ Monitor failed for {index_key}: {e}", exc_info=True)
            RUNTIME.health.note_error(e, where=f"monitor:{index_key}")
            events.error("monitor.failed", f"Could not check {index_key}",
                         str(e)[:300], index=index_key)

        # Angel rate-limits hard; pace the per-index calls.
        time.sleep(30)

    RUNTIME.health.set(
        last_cycle_at=datetime.now().isoformat(timespec="seconds"),
        last_cycle_ms=int((time.time() - cycle_started) * 1000),
        next_cycle_at=datetime.fromtimestamp(
            time.time() + MONITOR_INTERVAL).isoformat(timespec="seconds"),
    )

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
            handle_exit(STATE, index_key, "Stop loss triggered", df_oi)
            return
        if level == "TARGET":
            handle_exit(STATE, index_key, "Target achieved", df_oi)
            return

        # ── 2. End-of-day forced exit ──
        mins = minutes_to_close()
        if mins == 0:
            handle_exit(STATE, index_key, "EOD forced exit — market closed", df_oi)
            return
        if mins <= 30:
            handle_exit(STATE, index_key, f"EOD forced exit — {mins}min to close", df_oi)
            return
        if mins <= 60 and not position.get("_warned_60"):
            position["_warned_60"] = True
            events.info("position.closing_soon", f"{index_key} closes within the hour",
                f"The {position['strategy']} will be closed automatically "
                f"about 30 minutes before the market shuts.", index=index_key)

        # ── 3. LLM-advised exit / adjust (advisory) ──
        _log_gate_state(index_key, result, state="holding",
                        blocking="Already holding a position here")

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
    dte         = result.get("greeks", {}).get("days_to_exp")
    verdict = evaluate_entry(
        confluence      = confluence,
        risk_status     = risk_status,
        open_count      = pt.open_count(),
        max_positions   = rm.max_open_positions,
        in_entry_window = is_safe_to_enter(),
        blackout        = is_blackout(dte),
        in_cooldown     = pt.in_cooldown(index_key, _cooldown_minutes()),
        correlation     = rm.correlation_ok(index_key, confluence.get("overall_bias"), pt.open_trades),
    )
    # Every check is stored, not just the first failure, so the dashboard can
    # explain a quiet day instead of showing one cryptic line.
    _log_gate_state(index_key, result, state="scanned", verdict=verdict)

    if not verdict["allowed"]:
        logger.info(f"⚪ {index_key} no entry @ {now_str}: {verdict['blocking']}")
        return

    # Code approved the trade → ask the LLM ONLY as a final veto (advisory).
    if not _llm_approves_entry(STATE, index_key, result):
        return

    handle_enter(STATE, index_key, result)


def _log_gate_state(index_key: str, result: dict, state: str,
                    verdict: dict = None, blocking: str = None):
    """
    Persist why this index did or did not trade on this pass.

    Recording it for every state — scanned, holding, entered — means the
    dashboard always has an answer, instead of only when the gate ran.
    """
    if verdict is None:
        verdict = {"allowed": False, "blocking": blocking, "checks": []}
    try:
        log_gate(index_key, verdict,
                 confluence=result.get("confluence"),
                 spot=result.get("spot_ltp"), state=state)
    except Exception as e:
        logger.warning(f"⚠️ Could not record gate verdict for {index_key}: {e}")


def _llm_approves_entry(STATE: dict, index_key: str, result: dict) -> bool:
    """
    The code gate already approved. Ask the LLM once, as a veto: it can object
    (action == SKIP) but cannot force a trade. Also lets it suggest the strategy.
    Fail-open: if the LLM errors, the code-approved trade still proceeds.
    """
    from utils.llm_brain import get_trade_decision
    rm = STATE["risk_manager"]
    try:
        decision = get_trade_decision(
            summary     = result["summary"],
            greeks      = result["greeks"],
            regime      = result["regime"],
            confluence  = result["confluence"],
            risk_status = rm.get_status(),
            vix         = STATE.get("vix_ltp"),
            position    = None,
            ta          = result.get("ta"),
        )
    except Exception as e:
        logger.warning(f"⚠️ {index_key} LLM veto unavailable ({e}) — proceeding on code gate.")
        RUNTIME.health.set(llm_ok=False)
        return True

    RUNTIME.health.set(llm_ok=True,
                       llm_last_at=datetime.now().isoformat(timespec="seconds"))
    result["decision"] = decision   # let select_strategy honour its suggestion
    if decision.get("action") == "SKIP":
        reason = decision.get("reasoning", "")[:200]
        logger.info(f"🛑 {index_key} LLM vetoed entry: {reason}")
        events.warning("gate.llm_veto", f"AI second opinion said no to {index_key}",
                       reason, index=index_key)
        return False
    return True


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
    greeks     = result.get("greeks")

    # Reference premium for rough sizing (real margin is applied in approve_trade)
    ref_price = float(summary.get("atm_ce_ltp", 0)) + float(summary.get("atm_pe_ltp", 0))
    if ref_price <= 0:
        logger.warning(f"⚠️ {index_key} entry skipped — no ATM premium available.")
        return

    approval = rm.approve_trade(pt.capital, ref_price, lot_size,
                                open_count=pt.open_count())
    if not approval["approved"]:
        logger.warning(f"🚫 {index_key} entry blocked: {approval['reason']}")
        events.warning("trade.blocked", f"{index_key} trade blocked",
                       approval["reason"], index=index_key)
        return

    lots     = approval["lots"]
    strategy = strategies.select_strategy(confluence, regime, decision)

    # Refine lots against the REAL margin Angel would require for these legs
    lots = rm.cap_lots_by_margin(
        obj=STATE.get("obj"), index=index_key, strategy=strategy,
        summary=summary, df_oi=df_oi, options_df=options_df,
        lot_size=lot_size, expiry=result["expiry"],
        capital=pt.capital, max_lots=lots, greeks=greeks,
    )
    if lots < 1:
        logger.warning(f"🚫 {index_key} entry blocked: margin exceeds allocation.")
        events.warning("trade.blocked", f"{index_key} trade blocked",
            "The broker margin for this trade is more than the per-trade limit "
            "allows, even at one lot.", index=index_key)
        return

    position = strategies.build_position(
        index=index_key, strategy=strategy, summary=summary,
        df_oi=df_oi, options_df=options_df, lots=lots,
        lot_size=lot_size, expiry=result["expiry"], greeks=greeks,
    )
    if not position:
        events.warning("trade.blocked", f"{index_key} trade blocked",
            f"Could not price a tradable {strategy.replace('_', ' ')} — the "
            f"strikes were illiquid or the premium was too thin.", index=index_key)
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
    events.success(
        "trade.enter", f"Opened a {strategy.replace('_', ' ')} on {index_key}",
        f"Lots      : {lots} × {lot_size}\n"
        f"Legs:\n{legs_txt}\n"
        f"Net Prem  : ₹{position['net_credit']} ({position['direction']})\n"
        f"Stop Loss : ₹{position['stop_loss_pnl']} P&L\n"
        f"Target    : ₹{position['target_pnl']} P&L\n"
        f"📝 {decision.get('reasoning', '')[:200]}",
        index=index_key,
        meta={"strategy": strategy, "lots": lots, "lot_size": lot_size,
              "net_credit": position["net_credit"],
              "stop_loss_pnl": position["stop_loss_pnl"],
              "target_pnl": position["target_pnl"],
              "legs": [{"action": l["action"], "type": l["option_type"],
                        "strike": l["strike"], "entry": l["entry_ltp"]}
                       for l in position["legs"]]},
    )
    _log_gate_state(index_key, result, state="entered",
                    verdict={"allowed": True, "blocking": None, "checks": []})
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

    won = total_pnl >= 0
    emit = events.success if won else events.warning
    emit(
        "trade.exit",
        f"Closed the {trade['strategy'].replace('_', ' ')} on {index_key} "
        f"{'for a profit' if won else 'at a loss'}",
        f"Entry     : ₹{trade['entry_combined']}\n"
        f"Exit      : ₹{trade['exit_combined']}\n"
        f"P&L       : ₹{total_pnl}\n"
        f"Day Total : ₹{rm.daily_pnl.total_pnl}\n"
        f"Account   : ₹{pt.capital:,.0f}\n"
        f"📝 {reason}",
        index=index_key,
        meta={"strategy": trade["strategy"], "pnl": total_pnl, "reason": reason,
              "entry": trade["entry_combined"], "exit": trade["exit_combined"],
              "capital": round(pt.capital, 2)},
    )
    RUNTIME.series.drop(f"pnl:{index_key}")
    logger.info(f"🔴 {index_key} exit: P&L=₹{total_pnl}")


def handle_adjust(STATE: dict, index_key: str, decision: dict):
    pt  = STATE["paper_trader"]
    pos = pt.get_position(index_key)
    events.info(
        "position.adjust", f"Suggestion for the {index_key} position",
        f"Currently : {pos.get('strategy')}\n"
        f"Suggested : {decision.get('strategy')}\n"
        f"📝 {decision.get('reasoning')}",
        index=index_key,
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
            RUNTIME.health.note_error(e, where="monitor.cycle")
            try:
                events.error("monitor.failed", "The market check failed", str(e)[:300])
            except Exception:
                pass

        # Interruptible wait: a dashboard rescan wakes it straight away.
        _wake.wait(MONITOR_INTERVAL)
        _wake.clear()
