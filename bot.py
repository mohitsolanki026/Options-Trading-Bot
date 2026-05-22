import threading
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
from config.settings import INDICES, ACTIVE_INDEX
# Add at top of bot.py after imports
from utils.trade_journal import (
    init_db, log_signal, update_daily_summary,
    get_weekly_summary, print_today_signals
)
from utils.paper_trader import PaperTrader
from utils.websocket_feed import WebSocketFeed
from utils.tick_monitor import TickMonitor
from utils.index_scanner import scan_index

idx = INDICES[ACTIVE_INDEX]

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
STATE = {
    "obj":              None,
    "df_scrip":         None,
    "expiry":           None,
    "options_df":       None,
    "nifty_ltp":        None,
    "vix_ltp":          None,
    "summary":          None,
    "greeks":           None,
    "regime":           None,
    "confluence":       None,
    "decision":         None,
    "risk_manager":     None,
    "paper_trader":     PaperTrader(starting_capital=100000),  # ← add this
 # Per-index state — keyed by index name
    "index_data":        {},   # {"NIFTY": {...}, "BANKNIFTY": {...}}
    "current_positions": {},   # {"NIFTY": pos or None, "BANKNIFTY": None}
    "current_position":  None,
    "nifty_ltp":         None,
    "capital":          100000,
    "auth_token":  None,   # ← add
    "feed_token":  None,   # ← add
    "ws_feed":     None,   # ← add
    "ce_token":    None,   # ← add (ATM CE instrument token)
    "pe_token":    None,   # ← add (ATM PE instrument token)
}

def init_client():
    """Login to Angel One and load scrip master."""
    logger.info("🔐 Logging into Angel One...")
    obj, auth_token, feed_token = get_angel_client()   # ← three values now
    if not obj:
        send_error("Login FAILED!")
        raise Exception("Login failed")
    STATE["obj"]        = obj
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
    ws = WebSocketFeed(auth_token, feed_token)         # ← correct args
    ws.subscribe("NSE", ["26000", "99926017"])
    ws.start()
    STATE["ws_feed"] = ws
    logger.info("📡 WebSocket feed started.")

    # ── ADD THIS BLOCK AT THE BOTTOM ──────────────
    pt             = STATE["paper_trader"]
    saved_position = pt.load_position()

    if saved_position:
        STATE["current_position"] = saved_position
        send_alert(
            "⚠️ Open Position Found",
            f"Reloaded from previous session:\n"
            f"Strategy : {saved_position['strategy']}\n"
            f"Entry    : ₹{saved_position['combined_premium']}\n"
            f"Stop Loss: ₹{saved_position['stop_loss']}\n"
            f"Target   : ₹{saved_position['target']}\n"
            f"Expiry   : {saved_position['expiry']}\n"
            f"⚠️ Monitor will check this position immediately.",
            emoji="⚠️"
        )
    else:
        logger.info("✅ No open position from previous session.")

# ─────────────────────────────────────────
#  JOB 1 — Pre-Market Scan (8:45 AM)
# ─────────────────────────────────────────
def pre_market_scan():
    """
    Runs at 8:45 AM.
    Fetches global snapshot before market opens.
    """
    logger.info("🌅 Pre-market scan starting...")
    obj = STATE["obj"]

    nifty_ltp = fetch_ltp(obj, "NSE", "Nifty 50", "26000")
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    STATE["nifty_ltp"] = nifty_ltp
    STATE["vix_ltp"]   = vix_ltp

    send_market_update(nifty_ltp, vix_ltp)

    # VIX alert
    if vix_ltp and vix_ltp > 20:
        send_alert(
            "⚠️ HIGH VIX WARNING",
            f"VIX is {vix_ltp} — market is fearful.\nConsider buying options, avoid naked shorts.",
            emoji="😨"
        )

    logger.info(f"✅ Pre-market done. Nifty={nifty_ltp}, VIX={vix_ltp}")


# ─────────────────────────────────────────
#  JOB 2 — Market Open Scan (9:30 AM)
# ─────────────────────────────────────────
def market_open_scan():
    """Runs at 9:30 AM."""
    logger.info("📈 Market open scan starting...")
    obj        = STATE["obj"]
    options_df = STATE["options_df"]

    nifty_ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    STATE["nifty_ltp"] = nifty_ltp
    STATE["vix_ltp"]   = vix_ltp

    df_oi   = fetch_oi_data(obj, options_df, nifty_ltp, num_strikes=10)
    summary = summarise_options_chain(df_oi, nifty_ltp)

    # Subscribe ATM CE + PE tokens to WebSocket
    atm_strike = summary["atm_strike"]
    atm_ce = options_df[
        (options_df["strike"] == atm_strike * 100) &
        (options_df["symbol"].str.endswith("CE"))
    ]
    atm_pe = options_df[
        (options_df["strike"] == atm_strike * 100) &
        (options_df["symbol"].str.endswith("PE"))
    ]

    if not atm_ce.empty and not atm_pe.empty:
        ce_token = str(atm_ce.iloc[0]["token"])
        pe_token = str(atm_pe.iloc[0]["token"])

        STATE["ce_token"] = ce_token
        STATE["pe_token"] = pe_token

        ws = STATE.get("ws_feed")
        if ws:
            ws.subscribe("NFO", [ce_token, pe_token])
            logger.info(f"📡 Subscribed ATM: CE={ce_token} PE={pe_token}")

    STATE["summary"] = summary
    send_options_summary(summary)

    from utils.greeks_engine import analyse_atm_greeks
    greeks = analyse_atm_greeks(summary, STATE["expiry"])
    STATE["greeks"] = greeks

    # ── EXPIRY CHECK HERE (after greeks is defined) ──
    if greeks.get("days_to_exp", 1) == 0 and STATE.get("current_position"):
        logger.warning("⏰ EXPIRY DAY — closing open position immediately.")
        send_alert("⏰ Expiry Day Exit", "Closing open position — expiry day.", emoji="⏰")
        from utils.monitor import handle_exit
        handle_exit(STATE, {"reasoning": "Expiry day forced exit"}, nifty_ltp)

    # Send Greeks to Telegram
    from utils.telegram_helper import send_message
    greeks_msg = (
        f"🧮 <b>ATM Greeks — {STATE['expiry']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Days to Expiry : {greeks['days_to_exp']}\n"
        f"📊 Avg IV         : {greeks['avg_iv']}%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"CE IV    : {greeks['ce_iv']}%\n"
        f"PE IV    : {greeks['pe_iv']}%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Delta CE : {greeks['ce_delta']}\n"
        f"Delta PE : {greeks['pe_delta']}\n"
        f"Gamma    : {greeks['gamma']}\n"
        f"Theta    : ₹{greeks['theta']}/day\n"
        f"Vega     : {greeks['vega']}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(greeks_msg)
    logger.info(f"✅ Greeks: {greeks}")

    # Market Regime Detection
    from utils.regime_detector import detect_regime
    regime = detect_regime(
        vix            = STATE["vix_ltp"],
        pcr            = float(summary["pcr"]),
        days_to_expiry = greeks["days_to_exp"],
        nifty_spot     = summary["nifty_spot"],
        support        = float(summary["support"]),
        resistance     = float(summary["resistance"]),
        avg_iv         = float(greeks["avg_iv"] or 0.0),    )
    STATE["regime"] = regime

    # Signal Confluence
    from utils.signal_engine import run_confluence
    confluence = run_confluence(
        pcr            = float(summary["pcr"]),
        sentiment      = summary["sentiment"],
        support        = float(summary["support"]),
        resistance     = float(summary["resistance"]),
        nifty_spot     = summary["nifty_spot"],
        avg_iv         = float(greeks["avg_iv"]),
        vix            = STATE["vix_ltp"],
        days_to_expiry = greeks["days_to_exp"],
        theta          = float(greeks["theta"] or 0.0),
        regime         = regime["regime"],
    )
    STATE["confluence"] = confluence

    # Claude Brain Decision
    from utils.llm_brain import get_trade_decision
    from utils.risk_manager import RiskManager

    if not STATE.get("risk_manager"):
        STATE["risk_manager"] = RiskManager()

    risk_status = STATE["risk_manager"].get_status()

    decision = get_trade_decision(
        summary     = summary,
        greeks      = greeks,
        regime      = regime,
        confluence  = confluence,
        risk_status = risk_status,
        vix         = STATE["vix_ltp"],
        position    = STATE.get("current_position"),
    )
    STATE["decision"] = decision

    # Log everything to journal
    log_signal(
        index_name = idx["name"],
        summary    = summary,
        greeks     = greeks,
        regime     = regime,
        confluence = confluence,
        decision   = decision,
        vix        = STATE["vix_ltp"],
    )

    # Send decision to Telegram
    action_emoji = {
        "ENTER": "🟢", "HOLD": "🔵",
        "ADJUST": "🟡", "EXIT": "🔴", "SKIP": "⚪"
    }.get(decision.get("action"), "❓")

    decision_msg = (
        f"{action_emoji} <b>Claude AI Decision</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Action     : <b>{decision.get('action')}</b>\n"
        f"Confidence : {decision.get('confidence')}\n"
        f"Strategy   : {decision.get('strategy')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Stop Loss  : {decision.get('stop_loss')}\n"
        f"Target     : {decision.get('target')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Reasoning:</b>\n{decision.get('reasoning')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>Risk:</b> {decision.get('risk_warning')}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(decision_msg)
    logger.info(f"✅ Claude decision: {decision.get('action')}")

    # Build signal breakdown
    signal_lines = "\n".join([
        f"  {'✅' if s['score'] else '❌'} {s['label']}: {s['value']}"
        for s in confluence["signals"]
    ])

    confluence_msg = (
        f"🎯 <b>Signal Confluence</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Score  : {confluence['score']}/{confluence['max_score']}\n"
        f"Bias   : {confluence['overall_bias']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{signal_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{confluence['decision']}</b>\n"
        f"Strategy: {regime['strategy']}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(confluence_msg)
    logger.info(f"✅ Confluence: {confluence['score']}/{confluence['max_score']} → {confluence['decision']}")

    # Send regime to Telegram
    regime_msg = (
        f"{regime['emoji']} <b>Market Regime</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Regime     : <b>{regime['regime_label']}</b>\n"
        f"Confidence : {regime['confidence']}\n"
        f"Strategy   : 💡 {regime['strategy']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Reasons:</b>\n"
        + "\n".join([f"  • {r}" for r in regime["reasons"]]) +
        f"\n━━━━━━━━━━━━━━━━━━"
    )
    send_message(regime_msg)
    logger.info(f"✅ Regime detected: {regime['regime']}")

    # Key level alerts
    spot      = summary["nifty_spot"]
    support   = summary["support"]
    resistance = summary["resistance"]

    if spot < support + 50:
        send_alert(
            "🛡️ Near Support",
            f"Nifty ₹{spot} is near support {support}.\nWatch for bounce or breakdown.",
            emoji="🟡"
        )
    elif spot > resistance - 50:
        send_alert(
            "🚧 Near Resistance",
            f"Nifty ₹{spot} is near resistance {resistance}.\nWatch for breakout or rejection.",
            emoji="🟡"
        )

    logger.info(f"✅ Market open scan done. Summary: {summary}")


# ─────────────────────────────────────────
#  JOB 3 — Mid-Day Check (12:00 PM)
# ─────────────────────────────────────────
def midday_check():
    """
    Runs at 12:00 PM.
    Quick pulse check — just LTP + VIX.
    """
    logger.info("☀️ Mid-day check...")
    obj = STATE["obj"]

    # nifty_ltp = fetch_ltp(obj, "NSE", "Nifty 50", "26000")
    nifty_ltp = fetch_ltp(obj, "NSE", idx["symbol"], idx["token"])
    vix_ltp   = fetch_ltp(obj, "NSE", "India VIX", "99926017")

    send_market_update(nifty_ltp, vix_ltp)
    logger.info(f"✅ Mid-day: Nifty={nifty_ltp}, VIX={vix_ltp}")


# ─────────────────────────────────────────
#  JOB 4 — End of Day Report (3:30 PM)
# ─────────────────────────────────────────
def end_of_day_report():
    """Runs at 3:30 PM. Sends final summary for the day."""  # ← docstring first

    logger.info("🌆 End of day report...")
    
    update_daily_summary()   # ← then logic
    
    summary = STATE.get("summary", {})
    now     = datetime.now().strftime("%d %b %Y")

    msg = (
        f"📅 <b>End of Day — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Nifty Close  : ₹{STATE.get('nifty_ltp', 'N/A')}\n"
        f"VIX          : {STATE.get('vix_ltp', 'N/A')}\n"
        f"PCR          : {summary.get('pcr', 'N/A')}\n"
        f"Max Pain     : {summary.get('max_pain', 'N/A')}\n"
        f"Support      : {summary.get('support', 'N/A')}\n"
        f"Resistance   : {summary.get('resistance', 'N/A')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Bot shutting down for today."
    )
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
