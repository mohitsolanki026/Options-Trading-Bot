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
            return None, None, None

    except Exception as e:
        logger.error(f"❌ Exception during login: {e}")
        return None, None, None


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


def fetch_required_margin(obj, legs, lot_size: int = None, exchange: str = "NFO"):
    """
    Fetch the REAL margin Angel would block for a set of option legs, using the
    broker's margin calculator (``getMarginApi``). Read-only — places no order.

    legs     : list of {token, action ("BUY"|"SELL"), lots, entry_ltp}.
               Per-leg "lot_size" is used if present, else the ``lot_size`` arg.
    lot_size : index lot size, applied when legs don't carry their own.
    Returns total margin (float ₹) or None if the call fails (caller falls back).
    """
    if not obj or not legs:
        return None

    positions = []
    for leg in legs:
        leg_lot_size = leg.get("lot_size", lot_size)
        if not leg_lot_size:
            logger.error("❌ Margin calc: missing lot_size for leg.")
            return None
        qty = int(leg["lots"]) * int(leg_lot_size)
        positions.append({
            "exchange":    exchange,
            "qty":         qty,
            "price":       float(leg.get("entry_ltp", 0) or 0),
            "productType": "CARRY",   # positional index options
            "token":       str(leg["token"]),
            "tradeType":   "SELL" if leg["action"] == "SELL" else "BUY",
            "orderType":   "MARKET",
        })

    try:
        resp = obj.getMarginApi({"positions": positions})
        if resp and resp.get("status"):
            data = resp.get("data", {}) or {}
            margin = (data.get("totalMarginRequired")
                      or data.get("totalMargin")
                      or data.get("marginRequired"))
            if margin is not None:
                return float(margin)
        logger.warning(f"⚠️ Margin API returned no usable margin: {resp}")
    except Exception as e:
        logger.error(f"❌ Margin API error: {e}")
    return None


def fetch_market_data(obj, exchange, symbol, token):
    """Fetch full quote — OHLC, volume, OI etc."""
    try:
        data = obj.getMarketData("FULL", exchange, symbol, token)
        return data.get("data", {})
    except Exception as e:
        logger.error(f"❌ Market data error: {e}")
        return {}
