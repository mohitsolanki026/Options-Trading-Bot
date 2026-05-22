import time
import logging
from datetime import datetime
from utils.websocket_feed import TICK_STORE

logger = logging.getLogger(__name__)


class TickMonitor:
    """
    Monitors open positions using live WebSocket ticks
    instead of polling the API every 5 minutes.

    Stop loss and target checks now happen every second.
    """

    def __init__(self, STATE: dict, interval_seconds: int = 1):
        self.STATE    = STATE
        self.interval = interval_seconds
        self._running = False

    def start(self):
        """Start the tick monitoring loop in a background thread."""
        import threading
        t = threading.Thread(
            target = self._loop,
            daemon = True,
            name   = "TickMonitor"
        )
        t.start()
        logger.info("⚡ Tick monitor started — checking every 1 second.")

    def _loop(self):
        self._running = True
        while self._running:
            try:
                self._check_position()
            except Exception as e:
                logger.error(f"❌ Tick monitor error: {e}")
            time.sleep(self.interval)

    def _check_position(self):
        """Check open position against live tick prices."""
        pos = self.STATE.get("current_position")
        if not pos:
            return

        # Get live prices from WebSocket tick store
        ce_token = str(self.STATE.get("ce_token", ""))
        pe_token = str(self.STATE.get("pe_token", ""))

        ce_ltp = TICK_STORE.get_ltp(ce_token)
        pe_ltp = TICK_STORE.get_ltp(pe_token)

        # Fall back to last known API price if WebSocket not yet populated
        if ce_ltp == 0:
            summary = self.STATE.get("summary", {})
            ce_ltp  = float(summary.get("atm_ce_ltp", 0))
        if pe_ltp == 0:
            summary = self.STATE.get("summary", {})
            pe_ltp  = float(summary.get("atm_pe_ltp", 0))

        if ce_ltp == 0 or pe_ltp == 0:
            return

        combined = ce_ltp + pe_ltp
        sl       = pos.get("stop_loss", float("inf"))
        target   = pos.get("target", 0)
        now      = datetime.now().strftime("%H:%M:%S")

        # Log every 60 seconds (not every tick)
        if int(time.time()) % 60 == 0:
            logger.info(
                f"⚡ [{now}] Position: {pos['strategy']} | "
                f"CE={ce_ltp} PE={pe_ltp} Combined={combined:.2f} | "
                f"SL={sl} Target={target}"
            )

        # Stop loss hit
        if combined >= sl:
            logger.warning(f"🛑 SL HIT via tick: combined={combined:.2f} >= sl={sl}")
            self._trigger_exit("Stop loss hit via live tick", ce_ltp, pe_ltp)

        # Target hit
        elif combined <= target:
            logger.info(f"🎯 TARGET HIT via tick: combined={combined:.2f} <= target={target}")
            self._trigger_exit("Target achieved via live tick", ce_ltp, pe_ltp)

    def _trigger_exit(self, reason: str, ce_ltp: float, pe_ltp: float):
        """Trigger paper trade exit."""
        from utils.monitor import handle_exit
        from utils.telegram_helper import send_alert

        pos = self.STATE.get("current_position")
        if not pos:
            return

        send_alert("⚡ INSTANT EXIT", f"{reason}\nCE={ce_ltp} PE={pe_ltp}", emoji="⚡")
        handle_exit(
            self.STATE,
            {"reasoning": reason},
            ce_ltp + pe_ltp
        )
