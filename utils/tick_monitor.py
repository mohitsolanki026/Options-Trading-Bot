import time
import logging
from datetime import datetime
from utils.websocket_feed import TICK_STORE
from utils import strategies
from config.settings import ACTIVE_INDICES

logger = logging.getLogger(__name__)


class TickMonitor:
    """
    Monitors open positions using live WebSocket ticks.
    Checks every second for stop-loss and target hits across ALL active indices.
    Only fires during market hours.
    """

    def __init__(self, STATE: dict, interval_seconds: int = 1):
        self.STATE    = STATE
        self.interval = interval_seconds
        self._running = False
        self._last_log_time = 0

    def start(self):
        import threading
        t = threading.Thread(target=self._loop, daemon=True, name="TickMonitor")
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
                pt = self.STATE.get("paper_trader")
                if not pt:
                    time.sleep(self.interval)
                    continue

                if self._is_market_hours():
                    for index_key in list(pt.open_trades.keys()):
                        self._check_position(index_key)
                elif pt.open_count():
                    now_ts = int(time.time())
                    if now_ts - self._last_log_time >= 300:
                        logger.warning(
                            f"⚠️ {pt.open_count()} open position(s) outside market "
                            f"hours — will manage at market open."
                        )
                        self._last_log_time = now_ts
            except Exception as e:
                logger.error(f"❌ Tick monitor error: {e}")
            time.sleep(self.interval)

    def _check_position(self, index_key: str):
        """Check one index's open position against live tick prices."""
        pt  = self.STATE["paper_trader"]
        pos = pt.get_position(index_key)
        if not pos:
            return

        # Live ticks first; fall back to the latest options-chain snapshot
        df_oi = self.STATE.get("index_data", {}).get(index_key, {}).get("df_oi")
        price_map = strategies.current_price_map(pos, TICK_STORE, df_oi)
        if not price_map:
            return  # no fresh prices — cannot evaluate this tick

        pnl   = strategies.unrealised_pnl(pos, price_map)
        level = strategies.check_levels(pos, price_map)

        # Periodic status log (every 60s, once across positions)
        now_ts = int(time.time())
        if now_ts - self._last_log_time >= 60:
            logger.info(
                f"⚡ [{datetime.now().strftime('%H:%M:%S')}] {index_key} "
                f"{pos['strategy']} | P&L=₹{pnl} | "
                f"SL@₹{pos['stop_loss_pnl']} TGT@₹{pos['target_pnl']}"
            )
            self._last_log_time = now_ts

        if level == "STOP_LOSS":
            logger.warning(f"🛑 {index_key} SL HIT via tick: P&L=₹{pnl}")
            self._trigger_exit(index_key, "Stop loss hit via live tick", df_oi)
        elif level == "TARGET":
            logger.info(f"🎯 {index_key} TARGET HIT via tick: P&L=₹{pnl}")
            self._trigger_exit(index_key, "Target achieved via live tick", df_oi)

    def _trigger_exit(self, index_key: str, reason: str, df_oi):
        from utils.monitor import handle_exit
        from utils.telegram_helper import send_alert

        send_alert(f"⚡ {index_key} INSTANT EXIT", reason, emoji="⚡")
        handle_exit(self.STATE, index_key, reason, df_oi)
