"""The code-level entry gate must block on every failing condition."""
from utils.signal_engine import entry_allowed

GOOD_CONF = {"score": 5, "threshold": 4, "max_score": 7}
GOOD_RISK = {"trading_halted": False, "headroom": 5000, "halt_reason": None}


def _call(conf=None, risk=None, open_count=0, max_positions=3, in_window=True):
    return entry_allowed(
        confluence      = conf or GOOD_CONF,
        risk_status     = risk or GOOD_RISK,
        open_count      = open_count,
        max_positions   = max_positions,
        in_entry_window = in_window,
    )


def test_allows_when_all_conditions_pass():
    ok, reason = _call()
    assert ok and reason == "OK"


def test_blocks_low_score():
    ok, reason = _call(conf={"score": 3, "threshold": 4, "max_score": 7})
    assert not ok and "Score" in reason


def test_blocks_when_halted():
    ok, reason = _call(risk={"trading_halted": True, "headroom": 5000,
                             "halt_reason": "daily loss"})
    assert not ok and "halted" in reason.lower()


def test_blocks_when_no_headroom():
    ok, reason = _call(risk={"trading_halted": False, "headroom": 0})
    assert not ok and "loss limit" in reason.lower()


def test_blocks_at_max_positions():
    ok, reason = _call(open_count=3, max_positions=3)
    assert not ok and "Max positions" in reason


def test_blocks_outside_window():
    ok, reason = _call(in_window=False)
    assert not ok and "window" in reason.lower()
