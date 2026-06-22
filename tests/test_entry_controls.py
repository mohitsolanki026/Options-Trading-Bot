"""IV-rank history, event/expiry blackout, post-exit cooldown, correlation cap."""
from datetime import datetime, timedelta

from utils import iv_history as ivh
from utils import event_calendar as ec
from utils import paper_trader as ptmod
from utils.risk_manager import RiskManager


# ── IV history / rank ──

def test_iv_rank_unknown_until_min_history(monkeypatch, tmp_path):
    monkeypatch.setattr(ivh, "IV_HISTORY_FILE", str(tmp_path / "iv.json"))
    monkeypatch.setattr(ivh, "MIN_HISTORY", 3)
    ivh.record_iv("NIFTY", 14.0, today="2026-06-01")
    ivh.record_iv("NIFTY", 15.0, today="2026-06-02")
    assert ivh.get_iv_rank("NIFTY", 15.0) is None        # only 2 < 3


def test_iv_rank_percentile_after_history(monkeypatch, tmp_path):
    monkeypatch.setattr(ivh, "IV_HISTORY_FILE", str(tmp_path / "iv.json"))
    monkeypatch.setattr(ivh, "MIN_HISTORY", 3)
    for i, iv in enumerate([10, 12, 14, 16, 20]):
        ivh.record_iv("NIFTY", iv, today=f"2026-06-0{i+1}")
    assert ivh.get_iv_rank("NIFTY", 20) == 100.0          # current is the high
    assert ivh.get_iv_rank("NIFTY", 10) == 0.0            # current is the low


def test_vix_rank_seed_on_day_one(monkeypatch):
    """VIX percentile gives a usable IV-rank proxy before per-index history exists."""
    import pandas as pd
    monkeypatch.setattr(ivh, "MIN_HISTORY", 3)
    ivh._vix_cache["date"] = None
    closes = pd.DataFrame({"close": [10, 12, 14, 16, 20]})
    monkeypatch.setattr("utils.technical.fetch_candles", lambda *a, **k: closes)

    class Obj:  # any truthy object
        pass
    assert ivh.get_vix_rank(Obj(), 20) == 100.0
    assert ivh.get_vix_rank(Obj(), 10) == 0.0
    assert ivh.get_vix_rank(None, 15) is None     # no client → no proxy


def test_iv_same_day_updates_in_place(monkeypatch, tmp_path):
    monkeypatch.setattr(ivh, "IV_HISTORY_FILE", str(tmp_path / "iv.json"))
    ivh.record_iv("NIFTY", 14.0, today="2026-06-01")
    ivh.record_iv("NIFTY", 18.0, today="2026-06-01")     # same day → overwrite
    import json
    data = json.load(open(str(tmp_path / "iv.json")))
    assert len(data["NIFTY"]) == 1 and data["NIFTY"][0]["iv"] == 18.0


# ── event / expiry blackout ──

def test_expiry_day_blackout():
    blocked, reason = ec.is_blackout(days_to_expiry=0, today="2026-06-10")
    assert blocked and "expiry" in reason.lower()
    assert ec.is_blackout(days_to_expiry=5, today="2026-06-10")[0] is False


def test_event_date_blackout(monkeypatch):
    monkeypatch.setenv("EVENT_BLACKOUT_DATES", "2026-08-06,2026-09-30")
    assert ec.is_blackout(days_to_expiry=5, today="2026-08-06")[0] is True
    assert ec.is_blackout(days_to_expiry=5, today="2026-08-07")[0] is False


# ── post-exit cooldown ──

def test_cooldown_active_then_clears():
    pt = ptmod.PaperTrader(starting_capital=100000)
    pt.last_exit["NIFTY"] = datetime.now().isoformat()
    assert pt.in_cooldown("NIFTY", 30)[0] is True
    pt.last_exit["NIFTY"] = (datetime.now() - timedelta(minutes=45)).isoformat()
    assert pt.in_cooldown("NIFTY", 30)[0] is False
    assert pt.in_cooldown("BANKNIFTY", 30)[0] is False        # never traded


# ── correlation cap ──

def test_correlation_blocks_second_short_in_group():
    rm = RiskManager()
    open_trades = {"NIFTY": {"direction": "SELL"}}
    ok, _ = rm.correlation_ok("BANKNIFTY", "SELL_PREMIUM", open_trades)
    assert ok is False                                        # 2nd correlated short
    # A non-short (directional) trade on the correlated index is fine
    assert rm.correlation_ok("BANKNIFTY", "BULLISH", open_trades)[0] is True
    # First short with nothing open is fine
    assert rm.correlation_ok("NIFTY", "SELL_PREMIUM", {})[0] is True
