"""Integration: per-index entry respects the CODE gate, then the LLM veto."""
import pytest
from utils import monitor, paper_trader as ptmod, llm_brain
from utils.risk_manager import RiskManager
from tests.conftest import make_chain, make_summary


class FakeObj:
    def getMarginApi(self, params):
        return {"status": True, "data": {"totalMarginRequired": 100.0}}


def _result(score=5, bias="BULLISH", confirmed=True):
    df_oi, options_df = make_chain()
    return {
        "index": "NIFTY", "expiry": "26JUN2026",
        "options_df": options_df, "df_oi": df_oi,
        "spot_ltp": 24105, "summary": make_summary(),
        "greeks": {"theta": -10, "days_to_exp": 10, "avg_iv": 14.0},
        "regime": {"regime": "TRENDING_UP", "regime_label": "Trending Up"},
        "confluence": {"score": score, "threshold": 4, "max_score": 9,
                       "overall_bias": bias, "bias_confirmed": confirmed,
                       "bias_margin": 3, "premium_sell_ok": False},
        "decision": {"action": "NONE", "strategy": None, "reasoning": ""},
        "ta": {},
    }


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(monitor, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod, "log_trade_exit", lambda **k: None)
    monkeypatch.setattr(ptmod, "update_daily_summary", lambda: None)
    monkeypatch.setattr(ptmod.PaperTrader, "save_state", lambda self: None)
    monkeypatch.setattr(monitor, "is_safe_to_enter", lambda: True)
    monkeypatch.setattr(monitor, "minutes_to_close", lambda: 120)
    # LLM veto: approve by default
    monkeypatch.setattr(llm_brain, "get_trade_decision",
                        lambda **k: {"action": "ENTER", "strategy": "bull call spread",
                                     "reasoning": "ok"})
    return {
        "obj": FakeObj(),
        "paper_trader": ptmod.PaperTrader(starting_capital=500000),
        "risk_manager": RiskManager(),
        "ws_feed": None,
        "index_data": {},
        "vix_ltp": 14,
    }


def test_enters_when_gate_and_llm_pass(state):
    monitor.manage_index(state, "NIFTY", _result())
    assert state["paper_trader"].has_position("NIFTY")


def test_skips_when_score_below_threshold(state):
    monitor.manage_index(state, "NIFTY", _result(score=2))
    assert not state["paper_trader"].has_position("NIFTY")


def test_skips_when_bias_not_confirmed(state):
    monitor.manage_index(state, "NIFTY", _result(confirmed=False))
    assert not state["paper_trader"].has_position("NIFTY")


def test_llm_can_veto_a_code_approved_trade(state, monkeypatch):
    monkeypatch.setattr(llm_brain, "get_trade_decision",
                        lambda **k: {"action": "SKIP", "reasoning": "event risk"})
    monitor.manage_index(state, "NIFTY", _result())
    assert not state["paper_trader"].has_position("NIFTY")


def test_does_not_double_enter(state):
    monitor.manage_index(state, "NIFTY", _result())
    monitor.manage_index(state, "NIFTY", _result())
    assert state["paper_trader"].open_count() == 1
