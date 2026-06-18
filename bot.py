import threading
import time
from utils.monitor import start_monitor
import logging
from datetime import datetime
from utils.angel_helper import get_angel_client, fetch_ltp
from utils.options_helper import (
    download_scrip_master, get_available_expiries,
    get_nifty_options, fetch_oi_data, summarise_options_chain
)
from utils.telegram_helper import (
    send_market_update, send_options_summary,
    send_alert, send_error
)
from utils.scheduler import start_scheduler
from config.settings import INDICES, ACTIVE_INDICES, INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN
# Add at top of bot.py after imports
from utils.trade_journal import (
    init_db, log_signal, update_daily_summary,
    get_weekly_summary, print_today_signals
)
from utils.paper_trader import PaperTrader
from utils.websocket_feed import WebSocketFeed
from utils.tick_monitor import TickMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Shared state ---
# Open positions live in STATE["paper_trader"].open_trades (keyed by index) —
# that is the single source of truth. STATE["index_data"] holds the latest
# per-index analysis snapshot (incl. df_oi) for the tick monitor.
STATE = {
    "obj":           None,
    "df_scrip":      None,
    "expiry":        None,
    "options_df":    None,
    "nifty_ltp":     None,
    "vix_ltp":       None,
    "summary":       None,
    "greeks":        None,
    "regime":        None,
    "confluence":    None,
    "decision":      None,
    "risk_manager":  None,
    "paper_trader":  PaperTrader(starting_capital=100000),
    "index_data":    {},     # {"NIFTY": {analysis result}, ...}
    "auth_token":    None,
    "feed_token":    None,
    "ws_feed":       None,
}

def init_client():
    """Login to Angel One and load scrip master. Safe to re-run daily."""
    logger.info("🔐 Logging into Angel One...")
    obj, auth_token, feed_token = get_angel_client()   # (None, None, None) on failure
    if not obj:
        send_error("Login FAILED!")
        raise Exception("Login failed")
    STATE["obj"]        = obj
    STATE["auth_token"] = auth_token
    STATE["feed_token"] = feed_token

    logger.info("📥 Loading scrip master...")
    from datetime import datetime as dt
    df_scrip = download_scrip_master()
    STATE["df_scrip"] = df_scrip

    expiries = get_available_expiries(df_scrip)
    expiries_sorted = sorted(expiries, key=lambda e: (
        dt.strptime(e, "%d%b%Y") if len(e) == 9 else dt.max
    ))
    STATE["expiry"]     = expiries_sorted[0]
    STATE["options_df"] = get_nifty_options(df_scrip, STATE["expiry"])

    logger.info(f"✅ Using expiry: {STATE['expiry']}")
    send_alert("Bot Online", f"Logged in ✅\nExpiry: {STATE['expiry']}", emoji="🤖")

    # ── WebSocket feed — reuse if already running (avoid daily socket leak) ──
    old_ws = STATE.get("ws_feed")
    if old_ws is not None:
        try:
            old_ws.stop()
            logger.info("🛑 Stopped previous WebSocket before reconnect.")
        except Exception as e:
            logger.warning(f"⚠️ Could not stop old WebSocket: {e}")

    ws = WebSocketFeed(auth_token, feed_token)
    spot_tokens = [INDICES[i]["token"] for i in ACTIVE_INDICES] + [INDIA_VIX_TOKEN]
    ws.subscribe("NSE", spot_tokens)
    ws.start()
    STATE["ws_feed"] = ws
    logger.info("📡 WebSocket feed started.")

    # ── Restore any open paper positions and re-subscribe their leg tokens ──
    from utils import strategies
    pt          = STATE["paper_trader"]
    open_trades = pt.load_state()

    if open_trades:
        for index_key, pos in open_trades.items():
            ws.subscribe("NFO", strategies.all_tokens(pos))
            send_alert(
                f"⚠️ Open Position Restored [{index_key}]",
                f"Strategy : {pos['strategy']}\n"
                f"Net Prem : ₹{pos['net_credit']} ({pos['direction']})\n"
                f"Expiry   : {pos['expiry']}\n"
                f"Monitor will manage it immediately.",
                emoji="⚠️"
            )
    else:
        logger.info("✅ No open position from previous session.")

# ─────────────────────────────────────────
#  JOB 1 — Pre-Market Scan (8:45 AM)
# ─────────────────────────────────────────
def pre_market_scan():
    """Runs at 8:45 AM."""
    logger.info("🌅 Pre-market scan starting...")
    obj = STATE["obj"]

    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp

    lines = [f"🌅 <b>Pre-Market Snapshot</b>\n━━━━━━━━━━━━━━━━━━"]
    for index_key in ACTIVE_INDICES:
        idx     = INDICES[index_key]
        ltp     = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
        lines.append(f"<b>{index_key}</b> : ₹{ltp}")

    lines.append(f"VIX      : {vix_ltp}")
    lines.append(f"━━━━━━━━━━━━━━━━━━")

    if vix_ltp and vix_ltp > 20:
        lines.append("⚠️ HIGH VIX — avoid naked shorts")

    from utils.telegram_helper import send_message
    send_message("\n".join(lines))
    logger.info(f"✅ Pre-market done. VIX={vix_ltp}")


# ─────────────────────────────────────────
#  JOB 2 — Market Open Scan (9:30 AM)
# ─────────────────────────────────────────
def market_open_scan():
    """Runs at 9:30 AM — scans all active indices."""
    logger.info("📈 Market open scan starting...")
    obj     = STATE["obj"]
    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp

    if not STATE.get("risk_manager"):
        from utils.risk_manager import RiskManager
        STATE["risk_manager"] = RiskManager()

    from utils.index_scanner import scan_and_broadcast

    pt          = STATE["paper_trader"]
    risk_status = STATE["risk_manager"].get_status()

    results = {}
    for index_key in ACTIVE_INDICES:
        time.sleep(10)
        try:
            result = scan_and_broadcast(
                obj         = obj,
                df_scrip    = STATE["df_scrip"],
                index_key   = index_key,
                vix_ltp     = vix_ltp,
                risk_status = risk_status,
                position    = pt.get_position(index_key),
            )
            if not result:
                continue
            results[index_key] = result

        except Exception as e:
            logger.error(f"❌ Error scanning {index_key}: {e}")

    STATE["index_data"] = results

    # Best opportunity summary
    _send_best_opportunity(results)
    logger.info("✅ All indices scanned.")

def _send_best_opportunity(results: dict):
    """Highlight the best trade opportunity across all indices."""
    best       = None
    best_score = 0

    for key, result in results.items():
        if not result:
            continue
        score = result.get("confluence", {}).get("score", 0)
        if score > best_score:
            best_score = score
            best       = (key, result)

    if best and best_score >= 4:
        key, result = best
        max_score = result.get("confluence", {}).get("max_score", 7)
        send_alert(
            f"🏆 Best Opportunity: {key}",
            f"Score    : {best_score}/{max_score}\n"
            f"Regime   : {result['regime']['regime_label']}\n"
            f"Strategy : {result['decision'].get('strategy')}\n"
            f"Action   : {result['decision'].get('action')}",
            emoji="🏆"
        )


# ─────────────────────────────────────────
#  JOB 3 — Mid-Day Check (12:00 PM)
# ─────────────────────────────────────────
def midday_check():
    """
    Runs at 12:00 PM.
    Quick pulse check — LTP for every active index + VIX.
    """
    logger.info("☀️ Mid-day check...")
    obj = STATE["obj"]

    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp

    lines = [f"☀️ <b>Mid-Day Pulse</b>\n━━━━━━━━━━━━━━━━━━"]
    ltps = {}
    for index_key in ACTIVE_INDICES:
        idx = INDICES[index_key]
        ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
        ltps[index_key] = ltp
        lines.append(f"<b>{index_key}</b> : ₹{ltp}")
    lines.append(f"VIX      : {vix_ltp}")

    from utils.telegram_helper import send_message
    send_message("\n".join(lines))
    logger.info(f"✅ Mid-day: {ltps}, VIX={vix_ltp}")


# ─────────────────────────────────────────
#  JOB 4 — End of Day Report (3:30 PM)
# ─────────────────────────────────────────
def end_of_day_report():
    """Runs at 3:30 PM. Sends final summary for the day."""  # ← docstring first

    logger.info("🌆 End of day report...")

    update_daily_summary()   # ← then logic

    now        = datetime.now().strftime("%d %b %Y")
    index_data = STATE.get("index_data", {})

    lines = [
        f"📅 <b>End of Day — {now}</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"VIX          : {STATE.get('vix_ltp', 'N/A')}",
    ]
    for index_key in ACTIVE_INDICES:
        result  = index_data.get(index_key) or {}
        summary = result.get("summary", {})
        lines.append(f"\n<b>{index_key}</b>  Close ₹{result.get('spot_ltp', 'N/A')}")
        lines.append(f"  PCR {summary.get('pcr', 'N/A')} | "
                     f"MaxPain {summary.get('max_pain', 'N/A')}")
        lines.append(f"  Sup {summary.get('support', 'N/A')} | "
                     f"Res {summary.get('resistance', 'N/A')}")
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"✅ Bot shutting down for today.")
    msg = "\n".join(lines)

    from utils.telegram_helper import send_message
    weekly = get_weekly_summary()
    msg += f"\n\n📅 <b>Last 7 Days:</b>\n<pre>{weekly}</pre>"
    send_message(msg)
    logger.info("✅ End of day report sent.")
    pt = STATE.get("paper_trader")
    if pt:
        send_message(pt.get_stats_message())

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    # 1. Login + load data
    init_db()
    init_client()


    # 2. Run pre-market scan immediately on startup (for testing)
    pre_market_scan()
    market_open_scan()

    # 2b. Force-exit any restored position that has already expired
    from utils.monitor import force_exit_all_on_startup
    force_exit_all_on_startup(STATE)

    # 3. Schedule daily jobs
    jobs = {
        "08:30": init_client,
        "08:45": pre_market_scan,
        "09:30": market_open_scan,
        "12:00": midday_check,
        "15:30": end_of_day_report,
    }
    # Start 5-min monitor in background thread
    monitor_thread = threading.Thread(
        target = start_monitor,
        args   = (STATE,),
        daemon = True,
        name   = "MonitorLoop"
    )
    tick_mon = TickMonitor(STATE)
    tick_mon.start()
    logger.info("⚡ Tick monitor started.")
    monitor_thread.start()
    
    logger.info("👁️ Monitor thread started.")
    start_scheduler(jobs)
