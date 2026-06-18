"""Paper-trader full state must survive a restart (enter -> persist -> reload)."""
import os
from utils import paper_trader as ptmod
from utils import strategies
from tests.conftest import make_chain, make_summary


def _patch_journal_and_file(monkeypatch, tmp_path):
    """Stub out the sqlite journal and redirect the state file to tmp."""
    monkeypatch.setattr(ptmod, "log_trade_entry", lambda **k: 1)
    monkeypatch.setattr(ptmod, "log_trade_exit",  lambda **k: None)
    monkeypatch.setattr(ptmod, "update_daily_summary", lambda: None)
    monkeypatch.setattr(ptmod, "STATE_FILE", str(tmp_path / "paper_state.json"))


def _make_position(index="NIFTY", lots=2):
    df_oi, options_df = make_chain()
    return strategies.build_position(
        index=index, strategy="short_straddle", summary=make_summary(),
        df_oi=df_oi, options_df=options_df, lots=lots,
        lot_size=75, expiry="26JUN2025",
    )


def test_enter_persists_and_reloads(monkeypatch, tmp_path):
    _patch_journal_and_file(monkeypatch, tmp_path)

    pt = ptmod.PaperTrader(starting_capital=100000)
    pos = _make_position()
    pt.enter(pos)
    assert pt.open_count() == 1
    assert os.path.exists(ptmod.STATE_FILE)

    # Simulate a restart
    pt2 = ptmod.PaperTrader(starting_capital=999)   # wrong default — load should override
    pt2.load_state()
    assert pt2.open_count() == 1
    assert pt2.has_position("NIFTY")
    assert pt2.capital == 100000
    assert pt2.starting_capital == 100000


def test_exit_updates_capital_wins_and_persists(monkeypatch, tmp_path):
    _patch_journal_and_file(monkeypatch, tmp_path)

    pt = ptmod.PaperTrader(starting_capital=100000)
    pos = _make_position()
    pt.enter(pos)

    # Close at half premium → a winning short straddle
    price_map = {leg["token"]: leg["entry_ltp"] * 0.5 for leg in pos["legs"]}
    closed = pt.exit("NIFTY", price_map, reason="target")
    assert closed["pnl"] > 0
    assert pt.capital > 100000
    assert pt.wins == 1 and pt.losses == 0
    assert pt.open_count() == 0

    # Reload and confirm closed-trade history + capital survived
    pt2 = ptmod.PaperTrader()
    pt2.load_state()
    assert pt2.wins == 1
    assert len(pt2.trades) == 1
    assert pt2.capital == pt.capital


def test_double_exit_is_safe(monkeypatch, tmp_path):
    _patch_journal_and_file(monkeypatch, tmp_path)
    pt = ptmod.PaperTrader(starting_capital=100000)
    pos = _make_position()
    pt.enter(pos)
    pm = {leg["token"]: leg["entry_ltp"] * 0.5 for leg in pos["legs"]}
    first = pt.exit("NIFTY", pm, reason="first")
    second = pt.exit("NIFTY", pm, reason="second")   # no open trade now
    assert first and second == {}
    assert pt.wins == 1   # not double-counted
