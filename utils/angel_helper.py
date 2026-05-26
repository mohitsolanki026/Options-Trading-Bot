import pyotp
import logging
from SmartApi import SmartConnect
from config.settings import (
    ANGEL_API_KEY, ANGEL_CLIENT_ID,
    ANGEL_PASSWORD, ANGEL_TOTP_SECRET
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_angel_client():
    """
    Fully automated login — no browser needed.
    TOTP is generated programmatically every time.
    """
    try:
        obj = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()

        data = obj.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp
        )

        if data["status"]:
            auth_token  = data["data"]["jwtToken"]    # ← JWT token
            feed_token  = data["data"]["feedToken"]   # ← feed token
            logger.info("✅ Angel One login successful!")
            return obj, auth_token, feed_token 
        else:
            logger.error(f"❌ Login failed: {data}")
            return None

    except Exception as e:
        logger.error(f"❌ Exception during login: {e}")
        return None


def fetch_ltp(obj, exchange, symbol, token):
    """
    Fetch Last Traded Price.
    exchange: 'NSE' or 'NFO'
    symbol:   e.g. 'NIFTY'
    token:    instrument token e.g. '99926000' for Nifty
    """
    try:
        data = obj.ltpData(exchange, symbol, token)
        if data["status"]:
            return data["data"]["ltp"]
        else:
            logger.error(f"LTP fetch failed: {data}")
            return None
    except Exception as e:
        logger.error(f"❌ LTP error: {e}")
        return None


def fetch_market_data(obj, exchange, symbol, token):
    """Fetch full quote — OHLC, volume, OI etc."""
    try:
        data = obj.getMarketData("FULL", exchange, symbol, token)
        return data.get("data", {})
    except Exception as e:
        logger.error(f"❌ Market data error: {e}")
        return {}
