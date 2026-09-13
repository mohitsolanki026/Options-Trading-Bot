"""
Entry point: logs in, starts the background loops, and schedules the day.

Threads, all daemons so a Ctrl-C ends the process cleanly:
  * MonitorLoop  — the five-minute scan and management cycle
  * TickMonitor  — the one-second loop: stops, targets, chart sampling, commands
  * WebUI        — the dashboard, reading the same in-memory state
  * WebSocketFeed — Angel's live price stream
"""

import logging
import threading
import time
from datetime import datetime

from config.settings import (
    ACTIVE_INDICES, INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN, INDICES,
    PAPER_CAPITAL, WEB_UI_ENABLED,
)
from utils import events, settings_store
from utils.angel_helper import fetch_ltp, get_angel_client
from utils.monitor import active_indices, start_monitor
from utils.options_helper import (
    download_scrip_master, get_available_expiries, get_nifty_options,
)
from utils.paper_trader import PaperTrader
from utils.runtime import RUNTIME
from utils.scheduler import start_scheduler
from utils.tick_monitor import TickMonitor
from utils.trade_journal import (
    get_events, get_weekly_summary, init_db, record_equity, update_daily_summary,
)
from utils.websocket_feed import WebSocketFeed

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
# per-index analysis snapshot (incl. df_oi) for the tick monitor. The dashboard
# reads this same dict through utils.runtime.RUNTIME.
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
    "paper_trader":  PaperTrader(starting_capital=PAPER_CAPITAL),
    "index_data":    {},     # {"NIFTY": {analysis result}, ...}
    "active_indices": list(ACTIVE_INDICES),
    "auth_token":    None,
    "feed_token":    None,
    "ws_feed":       None,
}


def init_client():
    """Login to Angel One and load scrip master. Safe to re-run daily."""
    logger.info("🔐 Logging into Angel One...")
    obj, auth_token, feed_token = get_angel_client()   # (None, None, None) on failure
    if not obj:
        RUNTIME.health.set(broker_ok=False)
        RUNTIME.health.note_error("Angel One login failed", where="login")
        events.error("broker.login", "Could not sign in to Angel One",
                     "The bot cannot trade or read prices until this succeeds.")
        raise Exception("Login failed")

    STATE["obj"]        = obj
    STATE["auth_token"] = auth_token
    STATE["feed_token"] = feed_token
    RUNTIME.health.set(broker_ok=True,
                       broker_login_at=datetime.now().isoformat(timespec="seconds"))
    RUNTIME.health.clear_error()

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
    events.success("broker.login", "Bot is online",
                   f"Signed in to Angel One. Nearest expiry {STATE['expiry']}.")

    # ── WebSocket feed — reuse if already running (avoid daily socket leak) ──
    old_ws = STATE.get("ws_feed")
    if old_ws is not None:
        try:
            old_ws.stop()
            logger.info("🛑 Stopped previous WebSocket before reconnect.")
        except Exception as e:
            logger.warning(f"⚠️ Could not stop old WebSocket: {e}")

    ws = WebSocketFeed(auth_token, feed_token)
    indices = active_indices()
    STATE["active_indices"] = indices
    spot_tokens = [INDICES[i]["token"] for i in indices] + [INDIA_VIX_TOKEN]
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
            events.warning(
                "position.restored", f"Open {index_key} position restored",
                f"Strategy : {pos['strategy']}\n"
                f"Net Prem : ₹{pos['net_credit']} ({pos['direction']})\n"
                f"Expiry   : {pos['expiry']}\n"
                f"It is being managed again from now.",
                index=index_key,
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

    lines = []
    for index_key in active_indices():
        idx = INDICES[index_key]
        ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
        lines.append(f"{index_key} : ₹{ltp}")
    lines.append(f"VIX      : {vix_ltp}")
    if vix_ltp and vix_ltp > 20:
        lines.append("⚠️ High volatility — the bot will avoid naked shorts.")

    events.info("scan.premarket", "Pre-market snapshot", "\n".join(lines))
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
    indices = active_indices()
    STATE["active_indices"] = indices
    for index_key in indices:
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
            RUNTIME.health.note_error(e, where=f"scan:{index_key}")

    STATE["index_data"] = results
    _announce_best_opportunity(results)
    logger.info("✅ All indices scanned.")


def _announce_best_opportunity(results: dict):
    """Highlight the strongest read across all indices, when there is one."""
    best, best_score = None, 0
    for key, result in results.items():
        if not result:
            continue
        score = result.get("confluence", {}).get("score", 0)
        if score > best_score:
            best_score, best = score, (key, result)

    if best and best_score >= 4:
        key, result = best
        max_score = result.get("confluence", {}).get("max_score", 9)
        events.info(
            "scan.best", f"{key} looks the most interesting today",
            f"Score    : {best_score}/{max_score}\n"
            f"Regime   : {result['regime']['regime_label']}\n"
            f"Strategy : {result['decision'].get('strategy')}\n"
            f"Action   : {result['decision'].get('action')}",
            index=key,
        )


# ─────────────────────────────────────────
#  JOB 3 — Mid-Day Check (12:00 PM)
# ─────────────────────────────────────────
def midday_check():
    """Runs at 12:00 PM — a quick pulse on every active index plus VIX."""
    logger.info("☀️ Mid-day check...")
    obj = STATE["obj"]

    vix_ltp = fetch_ltp(obj, "NSE", INDIA_VIX_SYMBOL, INDIA_VIX_TOKEN)
    STATE["vix_ltp"] = vix_ltp

    lines, ltps = [], {}
    for index_key in active_indices():
        idx = INDICES[index_key]
        ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
        ltps[index_key] = ltp
        lines.append(f"{index_key} : ₹{ltp}")
    lines.append(f"VIX      : {vix_ltp}")

    events.info("scan.midday", "Midday pulse", "\n".join(lines))
    logger.info(f"✅ Mid-day: {ltps}, VIX={vix_ltp}")


# ─────────────────────────────────────────
#  JOB 4 — End of Day Report (3:30 PM)
# ─────────────────────────────────────────
def end_of_day_report():
    """Runs at 3:30 PM. Sends the final summary for the day."""
    logger.info("🌆 End of day report...")
    update_daily_summary()

    pt = STATE.get("paper_trader")
    if pt:
        record_equity(pt.capital)

    now        = datetime.now().strftime("%d %b %Y")
    index_data = STATE.get("index_data", {})

    lines = [f"VIX          : {STATE.get('vix_ltp', 'N/A')}"]
    for index_key in active_indices():
        result  = index_data.get(index_key) or {}
        summary = result.get("summary", {})
        lines.append(f"\n{index_key}  Close ₹{result.get('spot_ltp', 'N/A')}")
        lines.append(f"  PCR {summary.get('pcr', 'N/A')} | "
                     f"MaxPain {summary.get('max_pain', 'N/A')}")
        lines.append(f"  Sup {summary.get('support', 'N/A')} | "
                     f"Res {summary.get('resistance', 'N/A')}")

    weekly = get_weekly_summary()
    lines.append(f"\nLast 7 days:\n<pre>{weekly}</pre>")
    if pt:
        lines.append(f"\n{pt.get_stats_message()}")

    events.info("report.eod", f"End of day — {now}", "\n".join(lines))
    logger.info("✅ End of day report sent.")


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    init_db()

    # The dashboard reads this dict, and the activity feed starts with today's
    # history rather than an empty timeline after a restart.
    RUNTIME.bind_state(STATE)
    try:
        events.prime(get_events(limit=120, day=datetime.now().strftime("%Y-%m-%d")))
    except Exception as e:
        logger.warning(f"⚠️ Could not load today's activity: {e}")

    if WEB_UI_ENABLED:
        try:
            from web.server import start_web_server
            start_web_server()
        except Exception as e:
            # A dashboard problem must never stop the bot trading.
            logger.error(f"❌ Dashboard failed to start: {e}", exc_info=True)

    init_client()

    # Run the opening scans immediately so a restart mid-session is not blind.
    pre_market_scan()
    market_open_scan()

    # Force-exit any restored position that has already expired
    from utils.monitor import force_exit_all_on_startup
    force_exit_all_on_startup(STATE)

    jobs = {
        "08:30": init_client,
        "08:45": pre_market_scan,
        "09:30": market_open_scan,
        "12:00": midday_check,
        "15:30": end_of_day_report,
    }

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
