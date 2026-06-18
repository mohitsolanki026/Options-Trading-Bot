import time
import logging
from datetime import datetime
from utils.websocket_feed import TICK_STORE

logger = logging.getLogger(__name__)


class TickMonitor:
    """
    Monitors open positions using live WebSocket ticks.
    Checks every second for stop loss and target hits.
    Only fires during market hours.
    """

    def __init__(self, STATE: dict, interval_seconds: int = 1):
        self.STATE    = STATE
        self.interval = interval_seconds
        self._running = False
        self._last_log_time = 0

    def start(self):
        import threading
        t = threading.Thread(
            target = self._loop,
            daemon = True,
            name   = "TickMonitor"
        )
        t.start()
        logger.info("⚡ Tick monitor started — checking every 1 second.")

    def _is_market_hours(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return (9 * 60 + 30) <= t <= (15 * 60 + 30)

    def _loop(self):
        self._running = True
        while self._running:
            try:
                # Only check during market hours
                if self._is_market_hours():
                    self._check_position()
                # If market is closed and we have a position,
                # log a warning every 5 minutes
                elif self.STATE.get("current_position"):
                    now_ts = int(time.time())
                    if now_ts - self._last_log_time >= 300:
                        pos = self.STATE["current_position"]
                        logger.warning(
                            f"⚠️ Open position outside market hours: "
                            f"{pos['strategy']} — will exit at market open."
                        )
                        self._last_log_time = now_ts
            except Exception as e:
                logger.error(f"❌ Tick monitor error: {e}")
            time.sleep(self.interval)

    def _check_position(self):
        """Check open position against live WebSocket tick prices."""
        pos = self.STATE.get("current_position")
        if not pos:
            return

        ce_token = str(self.STATE.get("ce_token", ""))
        pe_token = str(self.STATE.get("pe_token", ""))

        ce_ltp = TICK_STORE.get_ltp(ce_token) if ce_token else 0
        pe_ltp = TICK_STORE.get_ltp(pe_token) if pe_token else 0

        # Fallback to summary prices if WebSocket not populated
        if ce_ltp == 0 or pe_ltp == 0:
            summary = self.STATE.get("summary", {})
            if not summary:
                return
            ce_ltp = float(summary.get("atm_ce_ltp", 0))
            pe_ltp = float(summary.get("atm_pe_ltp", 0))

        if ce_ltp == 0 or pe_ltp == 0:
            return

        combined = ce_ltp + pe_ltp
        sl       = pos.get("stop_loss", float("inf"))
        target   = pos.get("target", 0)
        now      = datetime.now().strftime("%H:%M:%S")

        # Log position status every 60 seconds
        now_ts = int(time.time())
        if now_ts - self._last_log_time >= 60:
            logger.info(
                f"⚡ [{now}] {pos['strategy']} | "
                f"CE=₹{ce_ltp} PE=₹{pe_ltp} Combined=₹{combined:.2f} | "
                f"SL=₹{sl} Target=₹{target}"
            )
            self._last_log_time = now_ts

        # Check levels
        if combined >= sl:
            logger.warning(f"🛑 SL HIT via tick: ₹{combined:.2f} >= ₹{sl}")
            self._trigger_exit("Stop loss hit via live tick", ce_ltp, pe_ltp)
        elif combined <= target:
            logger.info(f"🎯 TARGET HIT via tick: ₹{combined:.2f} <= ₹{target}")
            self._trigger_exit("Target achieved via live tick", ce_ltp, pe_ltp)

    def _trigger_exit(self, reason: str, ce_ltp: float, pe_ltp: float):
        """Trigger paper trade exit via monitor."""
        pos = self.STATE.get("current_position")
        if not pos:
            return

        from utils.monitor import handle_exit
        from utils.telegram_helper import send_alert

        send_alert("⚡ INSTANT EXIT",
            f"{reason}\nCE=₹{ce_ltp} PE=₹{pe_ltp}", emoji="⚡")
        handle_exit(
            self.STATE,
            {"reasoning": reason},
            ce_ltp + pe_ltp
        )