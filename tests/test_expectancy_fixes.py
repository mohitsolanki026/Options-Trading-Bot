"""
The fixes for a bot that was losing on structure: it flattened every trade
before the close, so sold premium never had time to decay while its stop was
hit by ordinary noise; it sized from a proxy and forced a lot; it read one
missing tick as a loss; and it filled at stale snapshot prices with no costs.
"""
from datetime import date, datetime, timedelta

import pytest

from utils import monitor, paper_trader as ptmod, settings_store, strategies
from utils import regime_detector, tick_monitor
from utils.risk_manager import RiskManager
from utils.websocket_feed import TICK_STORE
from tests.conftest import make_chain, make_summary
from tests.test_monitor_flow import FakeObj, _result


def _dmy(d: date) -> str:
    return d.strftime("%d%b%Y").upper()


def _tick(token, price):
    TICK_STORE.update(token, {"last_traded_price": int(round(price * 100))})


@pytest.fixture
def held(monkeypatch):
    """A state holding one NIFTY short straddle, priced from the chain snapshot."""
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod, "log_trade_exit", lambda **k: None)
    monkeypatch.setattr(ptmod, "update_daily_summary", lambda: None)
    monkeypatch.setattr(ptmod.PaperTrader, "save_state", lambda self: None)
    df_oi, options_df = make_chain()
    pt = ptmod.PaperTrader(starting_capital=500000)
    pos = strategies.build_position("NIFTY", "short_straddle", make_summary(), df_oi,
                                    options_df, lots=1, lot_size=75,
                                    expiry=_dmy(date.today() + timedelta(days=10)))
    pt.enter(pos)
    state = {"obj": FakeObj(), "paper_trader": pt, "risk_manager": RiskManager(),
             "ws_feed": None, "vix_ltp": 14,
             "index_data": {"NIFTY": {"df_oi": df_oi}}}
    result = _result()
    result["df_oi"] = df_oi
    return state, pos, result


# ── holding period ───────────────────────────────────────────────────────

def test_positional_holds_through_the_close(held, monkeypatch):
    state, pos, result = held
    monkeypatch.setattr(monitor, "minutes_to_close", lambda: 5)      # would have flattened
    monitor.manage_index(state, "NIFTY", result)
    assert state["paper_trader"].has_position("NIFTY")


def test_intraday_mode_still_flattens_before_the_close(held, monkeypatch):
    state, pos, result = held
    settings_store.update({"hold_mode": "intraday"})
    monkeypatch.setattr(monitor, "minutes_to_close", lambda: 20)
    monitor.manage_index(state, "NIFTY", result)
    assert not state["paper_trader"].has_position("NIFTY")


def test_positional_closes_on_expiry_morning(held):
    state, pos, result = held
    pos["expiry"] = _dmy(date.today())
    monitor.manage_index(state, "NIFTY", result)
    assert not state["paper_trader"].has_position("NIFTY")
    assert "Expiry day" in state["paper_trader"].trades[-1]["exit_reason"]


def test_positional_closes_at_the_hold_limit(held):
    state, pos, result = held
    settings_store.update({"max_hold_days": 3})
    pos["entry_time"] = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    monitor.manage_index(state, "NIFTY", result)
    assert not state["paper_trader"].has_position("NIFTY")
    assert "most allowed" in state["paper_trader"].trades[-1]["exit_reason"]


def test_hold_plan_text_follows_the_mode():
    assert "expiry" in monitor.hold_plan_text()
    settings_store.update({"hold_mode": "intraday"})
    assert "15:00" in monitor.hold_plan_text()


# ── a missing leg price must never fire a stop ───────────────────────────

def test_monitor_does_not_stop_out_on_a_half_priced_position(held):
    state, pos, result = held
    state["index_data"]["NIFTY"]["df_oi"] = None          # no snapshot fallback
    result["df_oi"] = None
    ce, pe = pos["legs"]
    _tick(ce["token"], ce["entry_ltp"] * 4)               # one leg looks disastrous...
    monitor.manage_index(state, "NIFTY", result)
    assert state["paper_trader"].has_position("NIFTY")    # ...but the other is unknown
    _tick(pe["token"], pe["entry_ltp"] * 4)               # now both are known and bad
    monitor.manage_index(state, "NIFTY", result)
    assert not state["paper_trader"].has_position("NIFTY")


def test_tick_monitor_waits_for_every_leg(held):
    state, pos, result = held
    state["index_data"]["NIFTY"]["df_oi"] = None
    tm = tick_monitor.TickMonitor(state)
    ce, pe = pos["legs"]
    _tick(ce["token"], ce["entry_ltp"] * 4)
    tm._check_position("NIFTY")
    assert state["paper_trader"].has_position("NIFTY")
    _tick(pe["token"], pe["entry_ltp"] * 4)
    tm._check_position("NIFTY")
    assert not state["paper_trader"].has_position("NIFTY")


def test_forced_exit_still_happens_and_flags_the_unpriced_leg(held, monkeypatch):
    state, pos, result = held
    state["index_data"]["NIFTY"]["df_oi"] = None
    result["df_oi"] = None
    pos["expiry"] = _dmy(date.today())                    # a time-based exit is due
    from utils import events
    monitor.manage_index(state, "NIFTY", result)
    assert not state["paper_trader"].has_position("NIFTY")
    kinds = [e["kind"] for e in events.recent()]
    assert "position.exit_unpriced" in kinds


def test_stale_ticks_expire():
    _tick("111", 42.0)
    assert TICK_STORE.get_ltp("111") == 42.0
    TICK_STORE._ticks["111"]["ts"] -= 600
    assert TICK_STORE.get_ltp("111", max_age=120) == 0.0
    assert TICK_STORE.get_ltp("111") == 42.0            # without a limit it is still there
    assert TICK_STORE.age("111") > 500


# ── sizing from the real trade ───────────────────────────────────────────

def test_lots_come_from_the_actual_stop_and_are_never_forced():
    rm = RiskManager(hydrate=False)
    rm.max_per_trade_loss = -10000
    assert rm.lots_for(-937.5, 1875, 500000)["lots"] == rm.max_lots_per_trade   # cap binds
    assert rm.lots_for(-5250, 120000, 500000)["lots"] == 1                     # risk binds
    zero = rm.lots_for(-17250, 150000, 500000)
    assert zero["lots"] == 0 and "more than" in zero["reason"]                 # skip, not 1
    assert rm.lots_for(-937.5, 200000, 500000)["lots"] == 1                    # margin binds
    assert rm.lots_for(-937.5, None, 500000)["lots"] == rm.max_lots_per_trade  # margin unknown


def test_entry_is_skipped_when_one_lot_exceeds_the_loss_limit(monkeypatch):
    from tests.test_monitor_flow import state as _state_fixture  # noqa: F401
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod.PaperTrader, "save_state", lambda self: None)
    monkeypatch.setattr(monitor, "is_safe_to_enter", lambda: True)
    state = {"obj": FakeObj(), "paper_trader": ptmod.PaperTrader(starting_capital=500000),
             "risk_manager": RiskManager(), "ws_feed": None, "index_data": {}, "vix_ltp": 14}
    # a sold straddle on the fake chain collects ₹230/unit → ₹17,250 stop per lot
    monitor.manage_index(state, "NIFTY", _result(score=5, bias="SELL_PREMIUM"))
    assert not state["paper_trader"].has_position("NIFTY")


def test_entry_uses_the_cap_and_live_prices_with_slippage(monkeypatch):
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod.PaperTrader, "save_state", lambda self: None)
    monkeypatch.setattr(monitor, "is_safe_to_enter", lambda: True)
    state = {"obj": FakeObj(), "paper_trader": ptmod.PaperTrader(starting_capital=500000),
             "risk_manager": RiskManager(), "ws_feed": None, "index_data": {}, "vix_ltp": 14}
    monitor.manage_index(state, "NIFTY", _result())
    pos = state["paper_trader"].get_position("NIFTY")
    assert pos["lots"] == 2                                   # cap, not the old forced 1
    assert pos["priced_from"] == "snapshot"                   # FakeObj has no quotes
    buy = [l for l in pos["legs"] if l["action"] == "BUY"][0]
    assert buy["entry_ltp"] > buy["snapshot_ltp"]             # bought a little worse
    assert pos["stop_loss_pnl"] == -round(pos["sl_pct"] * abs(pos["net_credit"]) * 75 * 2, 2)


def test_exit_deducts_slippage_and_charges(held):
    state, pos, result = held
    flat = {l["token"]: l["entry_ltp"] for l in pos["legs"]}
    closed = state["paper_trader"].exit("NIFTY", flat, reason="test")
    assert closed["charges"] == 25 * 2 * 2                    # two legs, in and out
    assert closed["pnl"] < 0 and closed["gross_pnl"] < 0      # flat market still costs money


# ── the pieces that had contradicted each other ──────────────────────────

def test_regime_reads_support_the_same_way_as_the_signals():
    r = regime_detector.detect_regime(vix=15, pcr=1.0, days_to_expiry=5, nifty_spot=24010,
                                      support=24000, resistance=24200, avg_iv=15)
    assert r["scores"]["TRENDING_UP"] >= 2 and r["scores"]["TRENDING_DOWN"] == 0
    assert any("bounce" in why for why in r["reasons"])
    assert "No new positions" in regime_detector.REGIME_STRATEGY["EXPIRY"]


def test_llm_strategy_suggestion_only_honoured_when_it_fits_the_view():
    sell = {"overall_bias": "SELL_PREMIUM"}
    bear = {"overall_bias": "BEARISH"}
    hint = {"strategy": "Short Straddle 24150"}
    assert strategies.select_strategy(sell, {}, hint) == "short_straddle"
    assert strategies.select_strategy(bear, {}, hint) == "bear_put_spread"
    assert strategies.normalise_strategy("Bull Call Spread 24100/24150") == "bull_call_spread"


def test_hold_mode_is_a_validated_choice():
    applied, errors = settings_store.update({"hold_mode": "weekly"})
    assert not applied and errors
    applied, _ = settings_store.update({"hold_mode": "INTRADAY"})
    assert applied == {"hold_mode": "intraday"}
