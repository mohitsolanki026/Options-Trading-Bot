"""Shared fixtures / builders for pure-logic tests (no network, no broker)."""
import os
import sys
import pandas as pd
import pytest

# Make the project root importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def isolated_bot(tmp_path, monkeypatch):
    """
    Give every test its own journal, settings file and event bus.

    Without this a test would read and write the real ``data/`` directory: the
    risk manager rehydrates today's P&L from the journal on construction, and
    the event bus would try to reach Telegram.
    """
    from utils import control, events, iv_history, paper_trader
    from utils import risk_manager, settings_store, trade_journal

    monkeypatch.setattr(trade_journal, "DB_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setattr(trade_journal, "_WAL_READY", False)
    monkeypatch.setattr(paper_trader, "STATE_FILE", str(tmp_path / "paper_state.json"))
    monkeypatch.setattr(risk_manager, "RISK_STATE_FILE", str(tmp_path / "risk_state.json"))
    monkeypatch.setattr(iv_history, "IV_HISTORY_FILE", str(tmp_path / "iv_history.json"))
    trade_journal.init_db()

    settings_store._reset_cache_for_tests(str(tmp_path / "settings.json"))
    settings_store.update({"telegram_enabled": False})   # never touch the network
    events._reset_for_tests()
    control._reset_for_tests()

    yield

    settings_store._reset_cache_for_tests("data/settings.json")


def make_chain(atm=24100, gap=50, ce_ltp=120.0, pe_ltp=110.0):
    """
    Build a fake (df_oi, options_df) pair around an ATM strike.
    df_oi     : strike, CE_LTP, PE_LTP, CE_OI, PE_OI
    options_df: strike, symbol, token
    """
    strikes = [atm + i * gap for i in range(-3, 4)]
    oi_rows, opt_rows = [], []
    for i, k in enumerate(strikes):
        # Prices taper away from ATM so spreads have distinct leg prices
        dist = abs(k - atm) // gap
        oi_rows.append({
            "strike": k,
            "CE_LTP": round(ce_ltp - dist * 25, 2),
            "PE_LTP": round(pe_ltp - dist * 25, 2),
            "CE_OI":  1000 + i * 10,
            "PE_OI":  1000 + i * 10,
        })
        for typ in ("CE", "PE"):
            opt_rows.append({
                "strike": k,
                "symbol": f"NIFTY{int(k)}{typ}",
                "token":  f"{int(k)}{1 if typ == 'CE' else 2}",
            })
    return pd.DataFrame(oi_rows), pd.DataFrame(opt_rows)


def make_summary(atm=24100):
    return {"atm_strike": atm, "nifty_spot": atm + 5, "atm_ce_ltp": 120, "atm_pe_ltp": 110}


@pytest.fixture
def chain():
    return make_chain()


@pytest.fixture
def summary():
    return make_summary()
