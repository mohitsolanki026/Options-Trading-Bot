import json, os
import logging
from datetime import datetime, date
from utils.trade_journal import (
    log_trade_entry, log_trade_exit, update_daily_summary
)

logger = logging.getLogger(__name__)
POSITION_FILE = "data/open_position.json"

class PaperTrader:
    """
    Simulates real trading without placing actual orders.
    Tracks full P&L history, win rate, and performance.
    """

    def __init__(self, starting_capital: float = 100000):
        self.starting_capital = starting_capital
        self.capital          = starting_capital
        self.trades           = []
        self.open_trade       = None
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        logger.info(f"📄 Paper Trader started with ₹{starting_capital:,}")


    # ─────────────────────────────────────
    #  ENTER TRADE
    # ─────────────────────────────────────

    def enter(
        self,
        index:      str,
        strategy:   str,
        strike:     float,
        ce_ltp:     float,
        pe_ltp:     float,
        lots:       int,
        lot_size:   int,
        expiry:     str,
        direction:  str = "SELL",
    ) -> dict:
        """Open a new paper trade."""

        if self.open_trade:
            logger.warning("⚠️ Already in a trade — cannot enter new one.")
            return {}

        combined_premium = round(ce_ltp + pe_ltp, 2)
        stop_loss        = round(combined_premium * 1.4, 2)
        target           = round(combined_premium * 0.5, 2)
        margin_used      = combined_premium * lots * lot_size * 0.2  # approx margin

        trade = {
            "id":                self.trade_count + 1,
            "index":             index,
            "strategy":          strategy,
            "strike":            strike,
            "direction":         direction,
            "ce_entry":          ce_ltp,
            "pe_entry":          pe_ltp,
            "combined_premium":  combined_premium,
            "lots":              lots,
            "lot_size":          lot_size,
            "expiry":            expiry,
            "stop_loss":         stop_loss,
            "target":            target,
            "margin_used":       margin_used,
            "entry_time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status":            "OPEN",
            "exit_premium":      None,
            "pnl":               None,
            "exit_time":         None,
            "exit_reason":       None,
        }

        self.open_trade   = trade
        self.trade_count += 1

        # Log to journal
        log_trade_entry(
            index_name  = index,
            strategy    = strategy,
            symbol      = f"NIFTY{strike}",
            direction   = direction,
            strike      = strike,
            option_type = "STRADDLE",
            entry_price = combined_premium,
            lots        = lots,
            lot_size    = lot_size,
            expiry      = expiry,
            notes       = f"CE={ce_ltp} PE={pe_ltp}",
        )

        logger.info(
            f"🟢 Paper ENTER: {strategy} strike={strike} "
            f"premium=₹{combined_premium} lots={lots}"
        )
        self.save_position()
        return trade


    # ─────────────────────────────────────
    #  EXIT TRADE
    # ─────────────────────────────────────

    def exit(
        self,
        ce_ltp:  float,
        pe_ltp:  float,
        reason:  str = "Manual exit",
    ) -> dict:
        """Close the current open paper trade."""

        if not self.open_trade:
            logger.warning("⚠️ No open trade to exit.")
            return {}

        trade           = self.open_trade
        exit_premium    = round(ce_ltp + pe_ltp, 2)
        lot_size        = trade["lot_size"]
        lots            = trade["lots"]

        # P&L for short straddle = sold premium - current premium
        pnl_per_lot  = (trade["combined_premium"] - exit_premium) * lot_size
        total_pnl    = round(pnl_per_lot * lots, 2)

        trade["exit_premium"] = exit_premium
        trade["pnl"]          = total_pnl
        trade["exit_time"]    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_reason"]  = reason
        trade["status"]       = "CLOSED"

        # Update capital
        self.capital += total_pnl

        # Track wins/losses
        if total_pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1

        self.trades.append(trade)
        self.open_trade = None

        # Log to journal
        log_trade_exit(
            trade_id   = trade["id"],
            exit_price = exit_premium,
            pnl        = total_pnl,
            notes      = reason,
        )
        update_daily_summary()

        logger.info(
            f"🔴 Paper EXIT: premium=₹{exit_premium} "
            f"P&L=₹{total_pnl} reason={reason}"
        )
        self.save_position()
        return trade


    # ─────────────────────────────────────
    #  STOP LOSS / TARGET CHECK
    # ─────────────────────────────────────

    def check_levels(self, ce_ltp: float, pe_ltp: float) -> str:
        """
        Check if current premiums hit stop loss or target.
        Returns: 'STOP_LOSS' | 'TARGET' | 'HOLD'
        """
        if not self.open_trade:
            return "HOLD"

        current = ce_ltp + pe_ltp
        sl      = self.open_trade["stop_loss"]
        target  = self.open_trade["target"]

        if current >= sl:
            logger.warning(f"🛑 Stop loss hit: ₹{current} >= ₹{sl}")
            return "STOP_LOSS"

        if current <= target:
            logger.info(f"🎯 Target hit: ₹{current} <= ₹{target}")
            return "TARGET"

        return "HOLD"


    # ─────────────────────────────────────
    #  PERFORMANCE STATS
    # ─────────────────────────────────────

    def get_stats(self) -> dict:
        """Return full performance statistics."""
        closed = [t for t in self.trades if t["status"] == "CLOSED"]

        total_pnl    = sum(t["pnl"] for t in closed)
        win_rate     = (self.wins / len(closed) * 100) if closed else 0
        avg_win      = (
            sum(t["pnl"] for t in closed if t["pnl"] > 0) / self.wins
            if self.wins else 0
        )
        avg_loss     = (
            sum(t["pnl"] for t in closed if t["pnl"] < 0) / self.losses
            if self.losses else 0
        )
        best_trade   = max((t["pnl"] for t in closed), default=0)
        worst_trade  = min((t["pnl"] for t in closed), default=0)
        profit_factor = (
            abs(sum(t["pnl"] for t in closed if t["pnl"] > 0)) /
            abs(sum(t["pnl"] for t in closed if t["pnl"] < 0))
            if self.losses else float("inf")
        )

        return {
            "starting_capital": self.starting_capital,
            "current_capital":  round(self.capital, 2),
            "total_pnl":        round(total_pnl, 2),
            "returns_pct":      round((self.capital - self.starting_capital) / self.starting_capital * 100, 2),
            "total_trades":     len(closed),
            "wins":             self.wins,
            "losses":           self.losses,
            "win_rate":         round(win_rate, 1),
            "avg_win":          round(avg_win, 2),
            "avg_loss":         round(avg_loss, 2),
            "best_trade":       best_trade,
            "worst_trade":      worst_trade,
            "profit_factor":    round(profit_factor, 2),
            "open_trade":       self.open_trade is not None,
        }


    def get_stats_message(self) -> str:
        """Formatted Telegram message of stats."""
        s = self.get_stats()
        emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"

        return (
            f"📄 <b>Paper Trading Stats</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Capital   : ₹{s['starting_capital']:,} → ₹{s['current_capital']:,}\n"
            f"Total P&L : {emoji} ₹{s['total_pnl']}\n"
            f"Returns   : {s['returns_pct']}%\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Trades    : {s['total_trades']}\n"
            f"Win Rate  : {s['win_rate']}%\n"
            f"Avg Win   : ₹{s['avg_win']}\n"
            f"Avg Loss  : ₹{s['avg_loss']}\n"
            f"Best      : ₹{s['best_trade']}\n"
            f"Worst     : ₹{s['worst_trade']}\n"
            f"Profit Factor: {s['profit_factor']}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
    
    def save_position(self):
        """Persist open position to disk."""
        os.makedirs("data", exist_ok=True)
        if self.open_trade:
            with open(POSITION_FILE, "w") as f:
                json.dump(self.open_trade, f, indent=2)
            logger.info("💾 Open position saved to disk.")
        else:
            # Clear file if no open position
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
                logger.info("🗑️ Position file cleared.")

    def load_position(self):
        """Load open position from disk on startup."""
        if not os.path.exists(POSITION_FILE):
            return None
        with open(POSITION_FILE, "r") as f:
            trade = json.load(f)
        self.open_trade  = trade
        self.trade_count = trade.get("id", 1)
        logger.warning(f"⚠️ Loaded open position from disk: {trade['strategy']} @ ₹{trade['combined_premium']}")
        return trade
