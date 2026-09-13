"""
The one-second loop.

It does three things, all of which need to happen far faster than the five
minute scan cycle:

  1. checks every open position against its stop loss and target on live ticks,
  2. samples the day's shape — each index's price and each position's running
     profit — into the runtime buffers the dashboard charts read, and
  3. runs whatever the dashboard has asked for.

Commands are executed here rather than in the web thread on purpose: this loop
already owns the shared state, so a manual close cannot race the automatic one.
"""

import logging
import threading
import time
from datetime import datetime

from config.settings import INDICES
from utils import control, events, strategies
from utils.runtime import RUNTIME
from utils.websocket_feed import TICK_STORE

logger = logging.getLogger(__name__)


class TickMonitor:
    """Watches open positions on live ticks across every active index."""

    def __init__(self, STATE: dict, interval_seconds: int = 1):
        self.STATE    = STATE
        self.interval = interval_seconds
        self._running = False
        self._thread  = None
        self._last_log_time = 0
        self._last_off_hours_log = 0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="TickMonitor")
        self._thread.start()
        logger.info("⚡ Tick monitor started — checking every 1 second.")

    def stop(self):
        self._running = False

    def _is_market_hours(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return (9 * 60 + 30) <= t <= (15 * 60 + 30)

    # ─────────────────────────────────────
    #  MAIN LOOP
    # ─────────────────────────────────────

    def _loop(self):
        self._running = True
        while self._running:
            try:
                # Commands run whether or not the market is open, so a stuck
                # position can always be closed and settings always take effect.
                self._run_commands()

                pt = self.STATE.get("paper_trader")
                if not pt:
                    time.sleep(self.interval)
                    continue

                if self._is_market_hours():
                    self._sample_spots()
                    for index_key in list(pt.open_trades.keys()):
                        self._check_position(index_key)
                elif pt.open_count():
                    now_ts = int(time.time())
                    if now_ts - self._last_off_hours_log >= 300:
                        logger.warning(
                            f"⚠️ {pt.open_count()} open position(s) outside market "
                            f"hours — will manage at market open."
                        )
                        self._last_off_hours_log = now_ts
            except Exception as e:
                logger.error(f"❌ Tick monitor error: {e}", exc_info=True)
                RUNTIME.health.note_error(e, where="tick_monitor")
            time.sleep(self.interval)

    # ─────────────────────────────────────
    #  SAMPLING  (what the charts draw)
    # ─────────────────────────────────────

    def _sample_spots(self):
        """Record each watched index's price, so the day's path is drawable."""
        for index_key in self.STATE.get("active_indices") or list(INDICES):
            idx = INDICES.get(index_key)
            if not idx:
                continue
            ltp = TICK_STORE.get_ltp(idx["token"])
            if ltp:
                RUNTIME.series.record(f"spot:{index_key}", ltp)

    # ─────────────────────────────────────
    #  POSITIONS
    # ─────────────────────────────────────

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
        RUNTIME.series.record(f"pnl:{index_key}", pnl)

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
            self._exit(index_key, "Stop loss hit on a live price", df_oi)
        elif level == "TARGET":
            logger.info(f"🎯 {index_key} TARGET HIT via tick: P&L=₹{pnl}")
            self._exit(index_key, "Target reached on a live price", df_oi)

    def _exit(self, index_key: str, reason: str, df_oi):
        from utils.monitor import handle_exit
        handle_exit(self.STATE, index_key, reason, df_oi)

    # ─────────────────────────────────────
    #  COMMANDS FROM THE DASHBOARD
    # ─────────────────────────────────────

    def _run_commands(self):
        for cmd in control.drain():
            try:
                ok, message = self._run_one(cmd)
            except Exception as e:
                logger.error(f"❌ Command {cmd['action']} failed: {e}", exc_info=True)
                ok, message = False, str(e)[:200]
                RUNTIME.health.note_error(e, where=f"command:{cmd['action']}")
            control.complete(cmd, ok, message)

    def _run_one(self, cmd: dict) -> tuple:
        action = cmd["action"]
        params = cmd.get("params") or {}
        pt = self.STATE.get("paper_trader")

        if action == "close_position":
            index_key = params.get("index")
            if not pt or not pt.has_position(index_key):
                return False, f"There is no open {index_key} trade to close."
            self._close_now(index_key)
            return True, f"Closed the {index_key} trade."

        if action == "close_all":
            if not pt or not pt.open_count():
                return False, "There are no open trades to close."
            closed = list(pt.open_trades.keys())
            for index_key in closed:
                self._close_now(index_key)
            return True, f"Closed {len(closed)} trade(s)."

        if action == "rescan":
            from utils.monitor import request_rescan
            request_rescan()
            events.info("control.rescan", "Checking the market now",
                        "You asked for a fresh check from the dashboard.")
            return True, "A fresh market check is starting."

        return False, f"Unknown command '{action}'."

    def _close_now(self, index_key: str):
        df_oi = self.STATE.get("index_data", {}).get(index_key, {}).get("df_oi")
        self._exit(index_key, "Closed from the dashboard", df_oi)
