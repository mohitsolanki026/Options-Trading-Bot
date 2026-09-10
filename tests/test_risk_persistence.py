"""A halt must outlive the process: a restart on a bad day cannot resume trading."""
import json
import os
from datetime import date, timedelta

from utils import risk_manager, settings_store, trade_journal
from utils.risk_manager import DailyPnL, RiskManager


def _blow_the_daily_limit(rm):
    """Close one trade big enough to breach the day's loss limit."""
    rm.close_position("NIFTY24000CE", rm.max_daily_loss - 1000)


def _saved_halt():
    with open(risk_manager.RISK_STATE_FILE) as f:
        return json.load(f)


def test_a_fresh_manager_is_not_halted():
    rm = RiskManager()
    assert rm.trading_halted is False
    assert rm.halt_reason is None
    assert not os.path.exists(risk_manager.RISK_STATE_FILE)


def test_breaching_the_daily_loss_limit_halts_and_persists():
    rm = RiskManager()
    _blow_the_daily_limit(rm)

    assert rm.trading_halted is True
    assert "Daily loss limit hit" in rm.halt_reason
    assert rm.halt_is_manual is False

    saved = _saved_halt()
    assert saved["trading_halted"] is True
    assert saved["date"] == date.today().isoformat()
    assert saved["manual"] is False


def test_a_restart_on_the_same_day_comes_back_halted():
    _blow_the_daily_limit(RiskManager())

    restarted = RiskManager()                   # as if the process crashed
    assert restarted.daily_pnl.total_pnl == 0.0  # journal has no closed trades
    assert restarted.trading_halted is True, \
        "a restart must not resume trading on a day whose loss limit was hit"
    assert "Daily loss limit hit" in restarted.halt_reason


def test_yesterdays_halt_is_not_carried_into_today():
    with open(risk_manager.RISK_STATE_FILE, "w") as f:
        json.dump({"date": (date.today() - timedelta(days=1)).isoformat(),
                   "trading_halted": True,
                   "halt_reason": "Daily loss limit hit yesterday",
                   "manual": False}, f)

    rm = RiskManager()
    assert rm.trading_halted is False
    assert rm.halt_reason is None


def test_manual_pause_persists_and_is_marked_manual():
    rm = RiskManager()
    assert rm.pause("Paused from the dashboard") is True
    assert rm.pause("again") is False           # already halted, nothing changed
    assert _saved_halt()["manual"] is True

    restarted = RiskManager()
    assert restarted.trading_halted is True
    assert restarted.halt_is_manual is True


def test_resume_is_refused_while_the_loss_limit_is_still_breached():
    rm = RiskManager()
    _blow_the_daily_limit(rm)

    ok, reason = rm.resume()
    assert ok is False
    assert "Daily loss limit hit" in reason
    assert rm.trading_halted is True
    assert _saved_halt()["trading_halted"] is True


def test_resume_works_once_the_day_is_back_inside_the_limit():
    rm = RiskManager()
    _blow_the_daily_limit(rm)
    rm.daily_pnl.add_trade(abs(rm.max_daily_loss) * 2, "NIFTY24000PE", "CLOSE")

    ok, message = rm.resume()
    assert ok is True and message == "Trading resumed"
    assert rm.trading_halted is False
    assert rm.halt_reason is None
    assert _saved_halt()["trading_halted"] is False

    assert RiskManager().trading_halted is False


def test_hydrate_restores_todays_realised_pnl_from_the_journal():
    for pnl in (-1200.0, 450.5):
        trade_id = trade_journal.log_trade_entry(
            index_name="NIFTY", strategy="short_straddle", symbol="NIFTY24000CE",
            direction="SELL", strike=24000, option_type="CE", entry_price=120.0,
            lots=1, lot_size=75, expiry="2026-09-10",
        )
        trade_journal.log_trade_exit(trade_id, exit_price=100.0, pnl=pnl)

    day = DailyPnL()
    assert day.total_pnl == -749.5
    assert day.trade_count == 2

    assert RiskManager().daily_pnl.total_pnl == -749.5
    assert DailyPnL(hydrate=False).total_pnl == 0.0


def test_refresh_picks_up_a_changed_limit_from_the_settings_store():
    rm = RiskManager()
    assert rm.max_open_positions == settings_store.get("max_open_positions")
    assert rm.refresh() is False                 # nothing changed yet

    settings_store.update({"max_open_positions": 7})
    assert rm.refresh() is True
    assert rm.max_open_positions == 7
    assert rm.check_position_limit()[0] is True
