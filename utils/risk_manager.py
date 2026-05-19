import logging
from datetime import datetime, date
from config.settings import RISK_RULES
from config.settings import INDICES, ACTIVE_INDEX

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  DAILY P&L TRACKER
# ─────────────────────────────────────────

class DailyPnL:
    """
    Tracks realized P&L for the current trading day.
    Resets automatically at midnight.
    """
    def __init__(self):
        self._date    = date.today()
        self._pnl     = 0.0
        self._trades  = []

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

    def __init__(self):
        self.daily_pnl        = DailyPnL()
        self.open_positions   = []   # list of active positions
        self.trading_halted   = False
        self.halt_reason      = None

        # Load rules from settings
        self.max_daily_loss       = RISK_RULES["max_daily_loss"]
        self.max_per_trade_loss   = RISK_RULES["max_per_trade_loss"]
        self.max_open_positions   = RISK_RULES["max_open_positions"]
        self.max_capital_per_trade = RISK_RULES["max_capital_per_trade"]

        logger.info("🛡️ Risk Manager initialised.")
        logger.info(f"   Max daily loss     : ₹{self.max_daily_loss}")
        logger.info(f"   Max per trade loss : ₹{self.max_per_trade_loss}")
        logger.info(f"   Max open positions : {self.max_open_positions}")
        logger.info(f"   Max capital/trade  : {self.max_capital_per_trade*100}%")


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

    def check_position_limit(self) -> tuple[bool, str]:
        """Are we already at max open positions?"""
        count = len(self.open_positions)
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

    def approve_trade(self, capital: float, option_price: float) -> dict:
        """
        Master approval function.
        Call this before placing ANY trade.
        Returns approved=True/False with reason.
        """
        checks = [
            self.check_trading_halted(),
            self.check_daily_loss_limit(),
            self.check_position_limit(),
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
        sizing = self.calculate_position_size(capital, option_price, INDICES[ACTIVE_INDEX]["lot_size"])

        return {
            "approved":  True,
            "reason":    "All risk checks passed",
            "lots":      sizing["recommended_lots"],
            "exposure":  sizing["total_exposure"],
            "sizing":    sizing,
        }


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

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Close a position and record P&L."""
        for pos in self.open_positions:
            if pos["symbol"] == symbol:
                lot_size =  INDICES[ACTIVE_INDEX]["lot_size"]
                if pos["direction"] == "SELL":
                    pnl = (pos["entry_price"] - exit_price) * pos["lots"] * lot_size
                else:
                    pnl = (exit_price - pos["entry_price"]) * pos["lots"] * lot_size

                self.open_positions.remove(pos)
                self.daily_pnl.add_trade(pnl, symbol, "CLOSE")

                logger.info(f"➖ Position closed: {symbol} P&L=₹{round(pnl, 2)}")

                # Auto-halt if daily limit breached
                ok, reason = self.check_daily_loss_limit()
                if not ok:
                    self.trading_halted = True
                    self.halt_reason    = reason
                    logger.warning(f"🛑 TRADING HALTED: {reason}")

                return round(pnl, 2)

        logger.warning(f"⚠️ Position not found: {symbol}")
        return 0.0

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
            "daily_pnl":        self.daily_pnl.total_pnl,
            "trade_count":      self.daily_pnl.trade_count,
            "open_positions":   len(self.open_positions),
            "daily_loss_limit": self.max_daily_loss,
            "headroom":         self.daily_pnl.total_pnl - self.max_daily_loss,
        }
