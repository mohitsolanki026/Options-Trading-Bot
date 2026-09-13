import json
import logging
import os
import threading
from datetime import datetime, date

from config.settings import RISK_RULES
from config.settings import INDICES, ACTIVE_INDICES, CORRELATION_GROUPS, MAX_CORRELATED_SHORT
from utils import settings_store

logger = logging.getLogger(__name__)

# A halt has to outlive the process. Before this, a crash or a restart after the
# daily loss limit was hit brought the bot back up with a clean slate and it
# happily kept trading on the same bad day.
RISK_STATE_FILE = "data/risk_state.json"


# ─────────────────────────────────────────
#  DAILY P&L TRACKER
# ─────────────────────────────────────────

class DailyPnL:
    """
    Tracks realized P&L for the current trading day.
    Resets automatically at midnight.
    """
    def __init__(self, hydrate: bool = True):
        self._date    = date.today()
        self._pnl     = 0.0
        self._trades  = []
        if hydrate:
            self.hydrate()

    def hydrate(self):
        """
        Load today's realised P&L from the trade journal.

        The journal is the source of truth for what actually happened today, so
        a restart mid-session resumes with the real number instead of zero.
        """
        try:
            from utils.trade_journal import get_today_realised
            pnl, count = get_today_realised(self._date.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.warning(f"⚠️ Could not read today's P&L from the journal: {e}")
            return
        self._pnl = float(pnl or 0.0)
        # Placeholders so trade_count reflects reality; details stay in the journal.
        self._trades = [{"time": "", "symbol": "", "action": "RESTORED", "pnl": None}
                        for _ in range(int(count or 0))]
        if self._pnl or self._trades:
            logger.info(f"↩️ Restored today's P&L: ₹{self._pnl:,.0f} "
                        f"over {len(self._trades)} closed trade(s).")

    def _check_reset(self):
        """Reset if it's a new day."""
        if date.today() != self._date:
            logger.info("🔄 New day — resetting P&L tracker.")
            self._date   = date.today()
            self._pnl    = 0.0
            self._trades = []

    def add_trade(self, pnl: float, symbol: str, action: str):
        self._check_reset()
        self._pnl += pnl
        self._trades.append({
            "time":   datetime.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "action": action,
            "pnl":    pnl,
        })
        logger.info(f"📝 Trade logged: {action} {symbol} P&L=₹{pnl} | Day total=₹{self._pnl}")

    @property
    def total_pnl(self):
        self._check_reset()
        return self._pnl

    @property
    def trade_count(self):
        self._check_reset()
        return len(self._trades)

    @property
    def trades(self):
        self._check_reset()
        return self._trades


# ─────────────────────────────────────────
#  RISK MANAGER
# ─────────────────────────────────────────

class RiskManager:
    """
    Central risk controller.
    Every trade MUST be approved by this before execution.
    """

    def __init__(self, hydrate: bool = True):
        self._lock            = threading.RLock()
        self.daily_pnl        = DailyPnL(hydrate=hydrate)
        self.open_positions   = []   # list of active positions
        self.trading_halted   = False
        self.halt_reason      = None
        self.halt_is_manual   = False
        self._settings_version = -1

        self.refresh()
        if hydrate:
            self._load_halt()

        logger.info("🛡️ Risk Manager initialised.")
        logger.info(f"   Max daily loss     : ₹{self.max_daily_loss}")
        logger.info(f"   Max per trade loss : ₹{self.max_per_trade_loss}")
        logger.info(f"   Max open positions : {self.max_open_positions}")
        logger.info(f"   Max capital/trade  : {self.max_capital_per_trade*100}%")

    # ── LIVE TUNABLES ─────────────────────

    def refresh(self):
        """
        Re-read the tunables if they changed. Called at the top of every monitor
        cycle so a limit edited in the dashboard applies without a restart.
        """
        try:
            version = settings_store.version()
            if version == self._settings_version:
                return False
            self.max_daily_loss        = settings_store.get("max_daily_loss")
            self.max_per_trade_loss    = settings_store.get("max_per_trade_loss")
            self.max_open_positions    = settings_store.get("max_open_positions")
            self.max_capital_per_trade = settings_store.get("max_capital_per_trade")
            self.max_correlated_short  = settings_store.get("max_correlated_short")
            self._settings_version     = version
            return True
        except Exception as e:
            logger.warning(f"⚠️ Falling back to env risk rules: {e}")
            self.max_daily_loss        = RISK_RULES["max_daily_loss"]
            self.max_per_trade_loss    = RISK_RULES["max_per_trade_loss"]
            self.max_open_positions    = RISK_RULES["max_open_positions"]
            self.max_capital_per_trade = RISK_RULES["max_capital_per_trade"]
            self.max_correlated_short  = MAX_CORRELATED_SHORT
            return False

    # ── DURABLE HALT STATE ────────────────

    def _load_halt(self):
        """Restore a halt from earlier today. Yesterday's halt is not carried over."""
        if not os.path.exists(RISK_STATE_FILE):
            return
        try:
            with open(RISK_STATE_FILE) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"❌ Could not read {RISK_STATE_FILE}: {e}")
            return
        if saved.get("date") != date.today().isoformat():
            return                      # a new day starts clean
        if saved.get("trading_halted"):
            self.trading_halted = True
            self.halt_reason    = saved.get("halt_reason")
            self.halt_is_manual = bool(saved.get("manual"))
            logger.warning(f"🛑 Trading is still halted from earlier today: {self.halt_reason}")

    def _save_halt(self):
        try:
            os.makedirs(os.path.dirname(RISK_STATE_FILE) or ".", exist_ok=True)
            tmp = RISK_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "date":           date.today().isoformat(),
                    "trading_halted": self.trading_halted,
                    "halt_reason":    self.halt_reason,
                    "manual":         self.halt_is_manual,
                    "saved_at":       datetime.now().isoformat(timespec="seconds"),
                }, f, indent=2)
            os.replace(tmp, RISK_STATE_FILE)
        except OSError as e:
            logger.error(f"❌ Could not save halt state: {e}")

    def pause(self, reason: str = "Paused from the dashboard", manual: bool = True):
        """Stop opening new trades. Open positions keep being managed."""
        with self._lock:
            if self.trading_halted:
                return False
            self.trading_halted = True
            self.halt_reason    = reason
            self.halt_is_manual = manual
            self._save_halt()
        logger.warning(f"🛑 Trading halted: {reason}")
        return True

    def resume(self) -> tuple:
        """
        Allow new trades again. Refuses while the daily loss limit is still
        breached, so a click cannot undo the account's own safety net.
        """
        with self._lock:
            ok, reason = self.check_daily_loss_limit()
            if not ok:
                return False, reason
            self.trading_halted = False
            self.halt_reason    = None
            self.halt_is_manual = False
            self._save_halt()
        logger.info("▶️ Trading resumed.")
        return True, "Trading resumed"


    # ── HARD STOP CHECKS ──────────────────

    def check_daily_loss_limit(self) -> tuple[bool, str]:
        """Has today's loss hit the daily limit?"""
        if self.daily_pnl.total_pnl <= self.max_daily_loss:
            reason = (
                f"Daily loss limit hit: ₹{self.daily_pnl.total_pnl} "
                f"<= ₹{self.max_daily_loss}"
            )
            return False, reason
        return True, "OK"

    def check_position_limit(self, open_count: int = None) -> tuple[bool, str]:
        """
        Are we already at max open positions?

        ``open_count`` should come from the paper trader, which is the single
        source of truth. This list is only a local cache and is empty after a
        restart, so trusting it would let the bot exceed its own position limit
        on the first trade after coming back up.
        """
        count = len(self.open_positions) if open_count is None else open_count
        if count >= self.max_open_positions:
            return False, f"Max positions reached: {count}/{self.max_open_positions}"
        return True, "OK"

    def check_no_trade_zone(self) -> tuple[bool, str]:
        """Is current time in a no-trade zone?"""
        now = datetime.now().strftime("%H:%M")
        no_trade_zones = [
            ("09:15", "09:29"),  # Opening noise
            ("15:00", "15:30"),  # Closing volatility
        ]
        for start, end in no_trade_zones:
            if start <= now <= end:
                return False, f"No-trade zone: {start}–{end}"
        return True, "OK"

    def check_trading_halted(self) -> tuple[bool, str]:
        """Has trading been manually or automatically halted?"""
        if self.trading_halted:
            return False, f"Trading halted: {self.halt_reason}"
        return True, "OK"


    # ── POSITION SIZING ───────────────────

    def calculate_position_size(
        self,
        capital: float,
        option_price: float,
        lot_size: int = 75
    ) -> dict:
        """
        Calculate how many lots to trade.

        capital      : total trading capital (e.g. ₹1,00,000)
        option_price : LTP of the option (e.g. ₹127)
        lot_size     : number of units per lot (e.g. 75 for Nifty)

        Returns max lots based on:
        1. Capital per trade limit (20%)
        2. Per trade loss limit
        """
        max_capital      = capital * self.max_capital_per_trade
        cost_per_lot     = option_price * lot_size
        max_loss_per_lot = abs(self.max_per_trade_loss)

        if cost_per_lot <= 0:
            return {"lots": 0, "reason": "Invalid option price"}

        # Method 1: Based on capital allocation
        lots_by_capital = int(max_capital / cost_per_lot)

        # Method 2: Based on max loss (assume worst case = 50% loss on premium)
        lots_by_risk = int(max_loss_per_lot / (cost_per_lot * 0.5))

        # Take the conservative (lower) number
        recommended_lots = max(1, min(lots_by_capital, lots_by_risk))

        result = {
            "capital":           capital,
            "max_capital":       max_capital,
            "option_price":      option_price,
            "lot_size":          lot_size,
            "cost_per_lot":      cost_per_lot,
            "lots_by_capital":   lots_by_capital,
            "lots_by_risk":      lots_by_risk,
            "recommended_lots":  recommended_lots,
            "total_exposure":    recommended_lots * cost_per_lot,
        }

        logger.info(
            f"📐 Position size: {recommended_lots} lot(s) "
            f"| Exposure: ₹{recommended_lots * cost_per_lot}"
        )
        return result


    # ── TRADE GATE (main approval function) ──

    def approve_trade(self, capital: float, option_price: float,
                      lot_size: int = None, open_count: int = None) -> dict:
        """
        Master approval function.
        Call this before placing ANY trade.
        Returns approved=True/False with reason.
        """
        checks = [
            self.check_trading_halted(),
            self.check_daily_loss_limit(),
            self.check_position_limit(open_count),
            self.check_no_trade_zone(),
        ]

        for passed, reason in checks:
            if not passed:
                logger.warning(f"🚫 Trade BLOCKED: {reason}")
                return {
                    "approved": False,
                    "reason":   reason,
                    "lots":     0,
                }

        # All checks passed — calculate size
        if lot_size is None:
            lot_size = INDICES[ACTIVE_INDICES[0]]["lot_size"]
        sizing = self.calculate_position_size(capital, option_price, lot_size)

        return {
            "approved":  True,
            "reason":    "All risk checks passed",
            "lots":      sizing["recommended_lots"],
            "exposure":  sizing["total_exposure"],
            "sizing":    sizing,
        }


    def correlation_ok(self, index: str, overall_bias: str, open_trades: dict) -> tuple:
        """
        Block a SHORT-premium entry if the index's correlation group already holds
        MAX_CORRELATED_SHORT short-vol positions. Two short straddles on NIFTY +
        BANKNIFTY is 2× the same vol bet, not diversification. Directional/long
        trades are not capped here. Returns (ok, reason).
        """
        if overall_bias != "SELL_PREMIUM":
            return True, ""
        group = next((g for g in CORRELATION_GROUPS if index in g), [index])
        holders = [idx for idx, pos in open_trades.items()
                   if idx in group and pos.get("direction") == "SELL"]
        if len(holders) >= getattr(self, "max_correlated_short", MAX_CORRELATED_SHORT):
            names = " and ".join(holders) if holders else "another index"
            return False, (f"You already hold a sold-options trade on {names}, which "
                           f"moves with {index}. Taking this too would double the same "
                           f"bet rather than spread it.")
        return True, ""

    def cap_lots_by_margin(self, obj, index, strategy, summary, df_oi, options_df,
                           lot_size, expiry, capital, max_lots, greeks=None) -> int:
        """
        Cap lot count so the REAL Angel margin for the chosen legs stays within
        the per-trade capital allocation. One margin API call; scales down
        proportionally if needed. Falls back to ``max_lots`` if the API is
        unavailable (paper/offline) so premium-based sizing still applies.
        """
        from utils import strategies
        from utils.angel_helper import fetch_required_margin

        allocation = capital * self.max_capital_per_trade

        position = strategies.build_position(
            index, strategy, summary, df_oi, options_df,
            max_lots, lot_size, expiry, greeks=greeks,
        )
        if not position:
            return 0

        margin = fetch_required_margin(obj, position["legs"], lot_size=lot_size)
        if margin is None or margin <= 0:
            logger.warning("⚠️ Real margin unavailable — using premium-based sizing.")
            return max_lots

        if margin <= allocation:
            logger.info(
                f"📐 {index} {strategy}: {max_lots} lot(s), margin ₹{margin:,.0f} "
                f"≤ alloc ₹{allocation:,.0f}"
            )
            return max_lots

        per_lot = margin / max_lots
        lots = int(allocation / per_lot)
        logger.info(
            f"📐 {index} {strategy}: margin ₹{margin:,.0f} > alloc ₹{allocation:,.0f} "
            f"→ trimmed {max_lots}→{lots} lot(s)"
        )
        return max(0, min(max_lots, lots))


    # ── POSITION MANAGEMENT ───────────────

    def add_position(self, symbol: str, entry_price: float, lots: int, direction: str):
        """Record a new open position."""
        position = {
            "symbol":      symbol,
            "entry_price": entry_price,
            "lots":        lots,
            "direction":   direction,  # 'BUY' or 'SELL'
            "entry_time":  datetime.now().strftime("%H:%M:%S"),
            "stop_loss":   entry_price * 0.5 if direction == "BUY" else entry_price * 1.5,
        }
        self.open_positions.append(position)
        logger.info(f"➕ Position added: {symbol} {direction} {lots} lot(s) @ ₹{entry_price}")
        return position

    def close_position(self, symbol: str, realized_pnl: float) -> float:
        """
        Record the close of a position. The realized P&L is computed by the
        execution layer (strategies/paper_trader) since it is strategy-aware
        (multi-leg); the risk manager only tracks the daily total and halts.
        """
        for pos in self.open_positions:
            if pos["symbol"] == symbol:
                self.open_positions.remove(pos)
                break
        else:
            logger.warning(f"⚠️ Position not found in risk manager: {symbol}")

        self.daily_pnl.add_trade(realized_pnl, symbol, "CLOSE")
        logger.info(f"➖ Position closed: {symbol} P&L=₹{round(realized_pnl, 2)}")

        # Auto-halt if daily limit breached
        ok, reason = self.check_daily_loss_limit()
        if not ok and not self.trading_halted:
            self.pause(reason, manual=False)

        return round(realized_pnl, 2)

    def check_stop_losses(self, current_prices: dict) -> list:
        """
        Check all open positions against stop losses.
        current_prices: {symbol: current_ltp}
        Returns list of positions that hit stop loss.
        """
        triggered = []
        for pos in self.open_positions:
            ltp = current_prices.get(pos["symbol"])
            if not ltp:
                continue

            sl = pos["stop_loss"]
            if pos["direction"] == "BUY" and ltp <= sl:
                triggered.append({**pos, "ltp": ltp, "reason": "Stop loss hit"})
                logger.warning(f"🛑 SL triggered: {pos['symbol']} LTP={ltp} <= SL={sl}")
            elif pos["direction"] == "SELL" and ltp >= sl:
                triggered.append({**pos, "ltp": ltp, "reason": "Stop loss hit"})
                logger.warning(f"🛑 SL triggered: {pos['symbol']} LTP={ltp} >= SL={sl}")

        return triggered

    def get_status(self) -> dict:
        """Return current risk status summary."""
        return {
            "trading_halted":   self.trading_halted,
            "halt_reason":      self.halt_reason,
            "halt_is_manual":   self.halt_is_manual,
            "daily_pnl":        self.daily_pnl.total_pnl,
            "trade_count":      self.daily_pnl.trade_count,
            "open_positions":   len(self.open_positions),
            "daily_loss_limit": self.max_daily_loss,
            "max_open_positions": self.max_open_positions,
            "headroom":         self.daily_pnl.total_pnl - self.max_daily_loss,
        }
