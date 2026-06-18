"""Integration: the monitor's per-index entry must respect the CODE gate."""
import pytest
from utils import monitor, paper_trader as ptmod
from utils.risk_manager import RiskManager
from tests.conftest import make_chain, make_summary


class FakeObj:
    def getMarginApi(self, params):
        # Tiny margin so sizing is never margin-constrained in the test
        return {"status": True, "data": {"totalMarginRequired": 100.0}}


def _result(score=5):
    df_oi, options_df = make_chain()
    return {
        "index": "NIFTY", "expiry": "26JUN2025",
        "options_df": options_df, "df_oi": df_oi,
        "spot_ltp": 24105, "summary": make_summary(),
        "greeks": {"theta": -10}, "regime": {"regime": "RANGE", "regime_label": "Range"},
        "confluence": {"score": score, "threshold": 4, "max_score": 7,
                       "overall_bias": "SELL PREMIUM"},
        "decision": {"action": "ENTER", "strategy": "Short Straddle",
                     "reasoning": "test"},
        "ta": {},
    }


@pytest.fixture
def state(monkeypatch):
    # Silence Telegram + sqlite journal
    monkeypatch.setattr(monitor, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod, "log_trade_exit", lambda **k: None)
    monkeypatch.setattr(ptmod, "update_daily_summary", lambda: None)
    monkeypatch.setattr(ptmod.PaperTrader, "save_state", lambda self: None)
    # Always inside the entry window, and far from EOD (avoid wall-clock effects)
    monkeypatch.setattr(monitor, "is_safe_to_enter", lambda: True)
    monkeypatch.setattr(monitor, "minutes_to_close", lambda: 120)

    return {
        "obj": FakeObj(),
        "paper_trader": ptmod.PaperTrader(starting_capital=100000),
        "risk_manager": RiskManager(),
        "ws_feed": None,
        "index_data": {},
    }


def test_enters_when_gate_passes(state):
    monitor.manage_index(state, "NIFTY", _result(score=5))
    pt = state["paper_trader"]
    assert pt.has_position("NIFTY")
    assert pt.get_position("NIFTY")["strategy"] == "short_straddle"


def test_skips_when_score_below_threshold(state):
    monitor.manage_index(state, "NIFTY", _result(score=2))
    assert not state["paper_trader"].has_position("NIFTY")


def test_does_not_double_enter(state):
    monitor.manage_index(state, "NIFTY", _result(score=5))
    monitor.manage_index(state, "NIFTY", _result(score=5))
    assert state["paper_trader"].open_count() == 1
