"""The code-level entry gate must block on every failing condition."""
from utils.signal_engine import entry_allowed

GOOD_CONF = {"score": 5, "threshold": 4, "max_score": 9, "bias_confirmed": True,
             "bias_margin": 3, "overall_bias": "BULLISH", "premium_sell_ok": False}
GOOD_RISK = {"trading_halted": False, "headroom": 5000, "halt_reason": None}


def _call(conf=None, risk=None, open_count=0, max_positions=3, in_window=True,
          blackout=(False, ""), cooldown=(False, ""), correlation=(True, "")):
    return entry_allowed(
        confluence      = conf or GOOD_CONF,
        risk_status     = risk or GOOD_RISK,
        open_count      = open_count,
        max_positions   = max_positions,
        in_entry_window = in_window,
        blackout        = blackout,
        in_cooldown     = cooldown,
        correlation     = correlation,
    )


def test_allows_when_all_conditions_pass():
    ok, reason = _call()
    assert ok and reason == "OK"


def test_blocks_low_score():
    ok, reason = _call(conf={**GOOD_CONF, "score": 3})
    assert not ok and "Score" in reason


def test_blocks_unconfirmed_bias():
    ok, reason = _call(conf={**GOOD_CONF, "bias_confirmed": False})
    assert not ok and "Bias not confirmed" in reason


def test_blocks_selling_cheap_premium():
    conf = {**GOOD_CONF, "overall_bias": "SELL_PREMIUM", "premium_sell_ok": False,
            "iv_rank": 20}
    ok, reason = _call(conf=conf)
    assert not ok and "IV rank too low" in reason


def test_allows_selling_rich_premium():
    conf = {**GOOD_CONF, "overall_bias": "SELL_PREMIUM", "premium_sell_ok": True}
    ok, _ = _call(conf=conf)
    assert ok


def test_blocks_when_halted():
    ok, reason = _call(risk={"trading_halted": True, "headroom": 5000, "halt_reason": "x"})
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


def test_blocks_on_blackout():
    ok, reason = _call(blackout=(True, "expiry day"))
    assert not ok and "blackout" in reason.lower()


def test_blocks_on_cooldown():
    ok, reason = _call(cooldown=(True, "20min left"))
    assert not ok and "cooldown" in reason.lower()


def test_blocks_on_correlated_exposure():
    ok, reason = _call(correlation=(False, "short already in group"))
    assert not ok and "Correlated" in reason
