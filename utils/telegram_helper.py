import logging
from datetime import datetime

import requests

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from utils.runtime import RUNTIME

logger = logging.getLogger(__name__)


def send_message(message: str, chat_id: str = None):
    """Send a plain text message to Telegram."""
    cid = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200:
            RUNTIME.health.set(telegram_last_at=datetime.now().isoformat(timespec="seconds"))
            logger.info("✅ Telegram message sent.")
            return True
        logger.error(f"❌ Telegram error: {r.text}")
    except Exception as e:
        logger.error(f"❌ Telegram exception: {e}")
    return False


def send_market_update(nifty_ltp, vix_ltp):
    """Send formatted market snapshot to Telegram."""
    msg = (
        f"📊 <b>Market Snapshot</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Nifty 50</b>  : ₹{nifty_ltp}\n"
        f"😨 <b>India VIX</b> : {vix_ltp}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(msg)


def send_alert(title: str, body: str, emoji: str = "🚨"):
    """Send a generic alert."""
    msg = (
        f"{emoji} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{body}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(msg)


def send_error(error_msg: str):
    """Send error notification."""
    send_alert("ERROR", error_msg, emoji="❌")

def send_options_summary(summary: dict):
    """Send options chain summary to Telegram."""
    sentiment_emoji = (
        "🟢" if summary["sentiment"] == "Bullish"
        else "🔴" if summary["sentiment"] == "Bearish"
        else "🟡"
    )
    msg = (
        f"📈 <b>Options Chain Summary</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Nifty Spot  : ₹{summary['nifty_spot']}\n"
        f"📍 ATM Strike  : {summary['atm_strike']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 PCR         : {summary['pcr']} ({sentiment_emoji} {summary['sentiment']})\n"
        f"😰 Max Pain    : {summary['max_pain']}\n"
        f"🛡️ Support     : {summary['support']}\n"
        f"🚧 Resistance  : {summary['resistance']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ATM CE LTP : ₹{summary['atm_ce_ltp']}\n"
        f"ATM PE LTP : ₹{summary['atm_pe_ltp']}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(msg)


def send_trade_alert(action: str, symbol: str, price: float, reason: str):
    """Send trade entry/exit alert."""
    emoji = "🟢" if action == "BUY" else "🔴"
    msg = (
        f"{emoji} <b>TRADE ALERT — {action}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 Symbol : {symbol}\n"
        f"💰 Price  : ₹{price}\n"
        f"📝 Reason : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    send_message(msg)
