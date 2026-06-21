import json, os
import logging
import threading
from datetime import datetime
from utils.trade_journal import (
    log_trade_entry, log_trade_exit, update_daily_summary
)
from utils import strategies

logger = logging.getLogger(__name__)
STATE_FILE = "data/paper_state.json"


class PaperTrader:
    """
    Simulates real trading without placing actual orders.

    Strategy-agnostic and multi-index: holds at most one open position PER index
    (keyed in ``self.open_trades``), each position being a list of legs built by
    ``utils.strategies``. Full state (capital, wins/losses, closed trades and all
    open positions) is persisted so results survive restarts.
    """

    def __init__(self, starting_capital: float = 100000):
        self.starting_capital = starting_capital
        self.capital          = starting_capital
        self.trades           = []      # closed trades
        self.open_trades      = {}      # index -> position dict
        self.last_exit        = {}      # index -> ISO timestamp of last exit
        self.trade_count      = 0
        self.wins             = 0
        self.losses           = 0
        self._lock            = threading.RLock()
        logger.info(f"📄 Paper Trader started with ₹{starting_capital:,}")

    # ─────────────────────────────────────
    #  ENTER
    # ─────────────────────────────────────

    def has_position(self, index: str) -> bool:
        return index in self.open_trades

    def get_position(self, index: str):
        return self.open_trades.get(index)

    def open_count(self) -> int:
        return len(self.open_trades)

    def enter(self, position: dict) -> dict:
        """
        Open a pre-built position (from ``strategies.build_position``).
        Returns the stored position, or {} if one is already open for that index.
        """
        with self._lock:
            index = position["index"]
            if index in self.open_trades:
                logger.warning(f"⚠️ Already in a trade for {index} — skipping entry.")
                return {}

            self.trade_count += 1
            position["id"]         = self.trade_count
            position["entry_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            legs_note = ", ".join(
                f"{l['action']} {l['option_type']}{int(l['strike'])}@{l['entry_ltp']}"
                for l in position["legs"]
            )
            journal_id = log_trade_entry(
                index_name  = index,
                strategy    = position["strategy"],
                symbol      = f"{index}{int(position['legs'][0]['strike'])}",
                direction   = position["direction"],
                strike      = position["legs"][0]["strike"],
                option_type = position["strategy"].upper(),
                entry_price = position["entry_combined"],
                lots        = position["lots"],
                lot_size    = position["lot_size"],
                expiry      = position["expiry"],
                notes       = legs_note,
            )
            position["journal_id"] = journal_id

            self.open_trades[index] = position
            logger.info(
                f"🟢 Paper ENTER [{index}]: {position['strategy']} "
                f"net=₹{position['net_credit']} lots={position['lots']} "
                f"SL@₹{position['stop_loss_pnl']} TGT@₹{position['target_pnl']}"
            )
            self.save_state()
            return position

    # ─────────────────────────────────────
    #  EXIT
    # ─────────────────────────────────────

    def exit(self, index: str, price_map: dict, reason: str = "Manual exit") -> dict:
        """Close the open position for ``index`` using {token: ltp} prices."""
        with self._lock:
            position = self.open_trades.get(index)
            if not position:
                logger.warning(f"⚠️ No open trade for {index} to exit.")
                return {}

            total_pnl = strategies.realise_pnl(position, price_map)
            position["pnl"]         = total_pnl
            position["exit_time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            position["exit_reason"] = reason
            position["status"]      = "CLOSED"

            self.capital += total_pnl
            if total_pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1

            self.trades.append(position)
            del self.open_trades[index]
            self.last_exit[index] = datetime.now().isoformat()

            log_trade_exit(
                trade_id   = position.get("journal_id", position["id"]),
                exit_price = position["exit_combined"],
                pnl        = total_pnl,
                notes      = reason,
            )
            update_daily_summary()

            logger.info(
                f"🔴 Paper EXIT [{index}]: exit=₹{position['exit_combined']} "
                f"P&L=₹{total_pnl} reason={reason}"
            )
            self.save_state()
            return position

    # ─────────────────────────────────────
    #  LEVEL CHECK
    # ─────────────────────────────────────

    def check_levels(self, index: str, price_map: dict) -> str:
        """Return 'STOP_LOSS' | 'TARGET' | 'HOLD' for an index's open position."""
        position = self.open_trades.get(index)
        if not position:
            return "HOLD"
        return strategies.check_levels(position, price_map)

    def in_cooldown(self, index: str, minutes: int) -> tuple:
        """(True, reason) if this index exited a trade within the last `minutes`."""
        ts = self.last_exit.get(index)
        if not ts:
            return False, ""
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
        except (ValueError, TypeError):
            return False, ""
        if elapsed < minutes:
            return True, f"{round(minutes - elapsed)}min left after last exit"
        return False, ""

    # ─────────────────────────────────────
    #  STATS
    # ─────────────────────────────────────

    def get_stats(self) -> dict:
        closed = self.trades
        total_pnl = sum(t["pnl"] for t in closed)
        win_rate  = (self.wins / len(closed) * 100) if closed else 0
        avg_win = (
            sum(t["pnl"] for t in closed if t["pnl"] > 0) / self.wins
            if self.wins else 0
        )
        avg_loss = (
            sum(t["pnl"] for t in closed if t["pnl"] < 0) / self.losses
            if self.losses else 0
        )
        best_trade  = max((t["pnl"] for t in closed), default=0)
        worst_trade = min((t["pnl"] for t in closed), default=0)
        gross_win   = sum(t["pnl"] for t in closed if t["pnl"] > 0)
        gross_loss  = sum(t["pnl"] for t in closed if t["pnl"] < 0)
        profit_factor = (
            abs(gross_win) / abs(gross_loss) if gross_loss else float("inf")
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
            "open_positions":   self.open_count(),
        }

    def get_stats_message(self) -> str:
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
            f"Open Pos  : {s['open_positions']}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

    # ─────────────────────────────────────
    #  PERSISTENCE (full state)
    # ─────────────────────────────────────

    def save_state(self):
        """Persist full trader state to disk."""
        with self._lock:
            os.makedirs("data", exist_ok=True)
            state = {
                "starting_capital": self.starting_capital,
                "capital":          self.capital,
                "trade_count":      self.trade_count,
                "wins":             self.wins,
                "losses":           self.losses,
                "trades":           self.trades,
                "open_trades":      self.open_trades,
                "last_exit":        self.last_exit,
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, STATE_FILE)   # atomic write
            logger.debug("💾 Paper state saved.")

    def load_state(self) -> dict:
        """
        Restore full state from disk on startup.
        Returns the dict of open positions (index -> position).
        """
        if not os.path.exists(STATE_FILE):
            logger.info("✅ No saved paper state — starting fresh.")
            return {}
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"❌ Could not load paper state: {e} — starting fresh.")
            return {}

        self.starting_capital = state.get("starting_capital", self.starting_capital)
        self.capital          = state.get("capital", self.starting_capital)
        self.trade_count      = state.get("trade_count", 0)
        self.wins             = state.get("wins", 0)
        self.losses           = state.get("losses", 0)
        self.trades           = state.get("trades", [])
        self.open_trades      = state.get("open_trades", {})
        self.last_exit        = state.get("last_exit", {})

        if self.open_trades:
            logger.warning(
                f"⚠️ Restored {len(self.open_trades)} open position(s): "
                f"{list(self.open_trades.keys())}"
            )
        logger.info(
            f"✅ Paper state restored: capital=₹{self.capital:,.0f} "
            f"trades={len(self.trades)} W/L={self.wins}/{self.losses}"
        )
        return self.open_trades
