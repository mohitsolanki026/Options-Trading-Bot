import threading
import logging
from datetime import datetime
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from config.settings import ANGEL_API_KEY, ANGEL_CLIENT_ID

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  TICK STORE
# ─────────────────────────────────────────

class TickStore:
    """Thread-safe store for latest tick data."""

    def __init__(self):
        self._lock  = threading.Lock()
        self._ticks = {}

    def update(self, token: str, data: dict):
        with self._lock:
            self._ticks[str(token)] = {
                "ltp":       data.get("last_traded_price", 0) / 100,
                "oi":        data.get("open_interest", 0),
                "volume":    data.get("volume_trade_for_the_day", 0),
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }

    def get(self, token: str) -> dict:
        with self._lock:
            return self._ticks.get(str(token), {})

    def get_ltp(self, token: str) -> float:
        return self.get(token).get("ltp", 0.0)

    def all_tokens(self) -> list:
        with self._lock:
            return list(self._ticks.keys())


# Global tick store
TICK_STORE = TickStore()


# ─────────────────────────────────────────
#  WEBSOCKET FEED
# ─────────────────────────────────────────

class WebSocketFeed:
    """Manages Angel One SmartWebSocketV2 connection."""

    MODE_LTP   = 1
    MODE_QUOTE = 2
    MODE_FULL  = 3

    def __init__(self, auth_token: str, feed_token: str):
        """
        auth_token : JWT token from generateSession
        feed_token : feed token from generateSession
        """
        self.auth_token  = auth_token
        self.feed_token  = feed_token
        self.sws         = None
        self._running    = False
        self._thread     = None
        self._subscribed = []  # list of {exchange, tokens} to subscribe on connect

        self.on_tick_callback = None  # optional external callback

    # ── CALLBACKS ────────────────────────

    def _on_open(self, wsapp):
        logger.info("✅ WebSocket connected.")
        self._running = True
        if self._subscribed:
            for sub in self._subscribed:
                self._do_subscribe(sub["exchange"], sub["tokens"])

    def _on_data(self, wsapp, *args):
        """
        Called on every tick.
        Angel One SDK calls this with varying args — use *args to be safe.
        args[0] = data dict, args[1] = data_type, args[2] = continue_flag
        """
        try:
            data = args[0] if args else {}
            if isinstance(data, dict):
                token = str(data.get("token", ""))
                if token:
                    TICK_STORE.update(token, data)
                    ltp = data.get("last_traded_price", 0) / 100
                    if int(data.get("token", 0)) in [26000, 99926017]:
                        logger.info(f"⚡ Tick: token={token} ltp=₹{ltp}")
                    if self.on_tick_callback:
                        self.on_tick_callback(token, data)
        except Exception as e:
            logger.error(f"❌ Tick processing error: {e}")

    def _on_error(self, wsapp, *args):
        error = args[0] if args else "Unknown error"
        logger.error(f"❌ WebSocket error: {error}")

    def _on_close(self, wsapp, *args):
        logger.warning("⚠️ WebSocket closed.")
        self._running = False
    # ── SUBSCRIBE ────────────────────────

    def _exchange_type(self, exchange: str) -> int:
        return {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}.get(
            exchange.upper(), 1
        )

    def _do_subscribe(self, exchange: str, tokens: list):
        """Actually send subscription to WebSocket."""
        if not self.sws:
            return
        token_list = [{
            "exchangeType": self._exchange_type(exchange),
            "tokens":       [str(t) for t in tokens],
        }]
        self.sws.subscribe("trading_bot", self.MODE_FULL, token_list)
        logger.info(f"📡 Subscribed: {exchange} → {tokens}")

    def subscribe(self, exchange: str, tokens: list):
        """
        Queue tokens for subscription.
        If already connected → subscribe immediately.
        If not yet connected → will subscribe on open.
        """
        sub = {"exchange": exchange, "tokens": [str(t) for t in tokens]}
        self._subscribed.append(sub)

        if self._running and self.sws:
            self._do_subscribe(exchange, tokens)
        else:
            logger.info(f"⏳ Queued {exchange} tokens — will subscribe on connect.")

    # ── START / STOP ─────────────────────

    def start(self):
        """Start WebSocket in background thread."""
        try:
            self.sws = SmartWebSocketV2(
                auth_token         = self.auth_token,
                api_key            = ANGEL_API_KEY,
                client_code        = ANGEL_CLIENT_ID,
                feed_token         = self.feed_token,
                max_retry_attempt  = 5,
                retry_strategy     = 0,
                retry_delay        = 10,
                retry_multiplier   = 2,
                retry_duration     = 60,
            )

            # Assign our callbacks
            self.sws.on_open  = self._on_open
            self.sws.on_data  = self._on_data
            self.sws.on_error = self._on_error
            self.sws.on_close = self._on_close

            self._thread = threading.Thread(
                target = self.sws.connect,
                daemon = True,
                name   = "WebSocketFeed"
            )
            self._thread.start()
            logger.info("🚀 WebSocket feed thread started.")

        except Exception as e:
            logger.error(f"❌ WebSocket start failed: {e}")

    def stop(self):
        if self.sws:
            self.sws.close_connection()
            self._running = False
            logger.info("🛑 WebSocket stopped.")

    @property
    def is_running(self) -> bool:
        return self._running