"""The entry gate must report EVERY failing condition, not just the first one."""
from utils.signal_engine import entry_allowed, evaluate_entry

GOOD_CONF = {"score": 5, "threshold": 4, "max_score": 9, "bias_confirmed": True,
             "bias_margin": 3, "overall_bias": "BULLISH", "premium_sell_ok": False}
GOOD_RISK = {"trading_halted": False, "headroom": 5000, "halt_reason": None}

# The order the checks are declared in — the gate reports the first of these
# that fails as the headline reason.
ORDER = ["score", "bias", "premium", "halted", "headroom", "slots",
         "window", "blackout", "cooldown", "correlation"]


def _verdict(conf=None, risk=None, open_count=0, max_positions=3, in_window=True,
             blackout=(False, ""), cooldown=(False, ""), correlation=(True, "")):
    return evaluate_entry(
        confluence      = conf or GOOD_CONF,
        risk_status     = risk or GOOD_RISK,
        open_count      = open_count,
        max_positions   = max_positions,
        in_entry_window = in_window,
        blackout        = blackout,
        in_cooldown     = cooldown,
        correlation     = correlation,
    )


def test_a_clean_run_passes_every_check():
    v = _verdict()
    assert v["allowed"] is True
    assert v["blocking"] is None
    assert v["failed"] == []
    assert v["total"] == 10 and v["passed"] == 10
    assert len(v["checks"]) == 10
    assert all(c["passed"] for c in v["checks"])


def test_every_failure_is_reported_not_just_the_first():
    v = _verdict(blackout=(True, "expiry day"),
                 correlation=(False, "1 short-vol already in group"))
    assert v["allowed"] is False
    assert len(v["failed"]) == 2
    assert set(v["failed"]) == {"blackout", "correlation"}
    assert v["passed"] == 8 and v["total"] == 10


def test_four_simultaneous_failures_are_all_reported():
    v = _verdict(conf={**GOOD_CONF, "score": 1},
                 risk={"trading_halted": True, "headroom": 0, "halt_reason": "bad day"},
                 in_window=False)
    assert set(v["failed"]) == {"score", "halted", "headroom", "window"}
    assert v["passed"] == 6


def test_blocking_is_the_first_failure_in_declaration_order():
    v = _verdict(conf={**GOOD_CONF, "score": 1},
                 risk={"trading_halted": True, "headroom": 5000, "halt_reason": "x"},
                 blackout=(True, "expiry day"))
    assert v["failed"][0] == "score"
    assert "Score" in v["blocking"]
    # ...and the reported order matches the declaration order.
    assert v["failed"] == [k for k in ORDER if k in v["failed"]]


def test_bias_outranks_premium_as_the_blocking_reason():
    v = _verdict(conf={**GOOD_CONF, "bias_confirmed": False,
                       "overall_bias": "SELL_PREMIUM", "premium_sell_ok": False,
                       "iv_rank": 12})
    assert v["failed"] == ["bias", "premium"]
    assert "Bias not confirmed" in v["blocking"]


def test_premium_check_is_not_applicable_when_not_selling():
    premium = next(c for c in _verdict()["checks"] if c["key"] == "premium")
    assert premium["na"] is True
    assert premium["passed"] is True
    assert "does not apply" in premium["detail"]


def test_premium_check_applies_when_the_bias_is_to_sell():
    v = _verdict(conf={**GOOD_CONF, "overall_bias": "SELL_PREMIUM",
                       "premium_sell_ok": False, "iv_rank": 20})
    premium = next(c for c in v["checks"] if c["key"] == "premium")
    assert premium["na"] is False
    assert premium["passed"] is False
    assert "20" in premium["detail"]
    assert v["failed"] == ["premium"]


def test_every_check_explains_itself_in_plain_english():
    for v in (_verdict(),
              _verdict(conf={**GOOD_CONF, "score": 0, "bias_confirmed": False},
                       risk={"trading_halted": True, "headroom": 0, "halt_reason": "x"},
                       open_count=3, in_window=False,
                       blackout=(True, "budget day"), cooldown=(True, "20 min left"),
                       correlation=(False, "already short NIFTY"))):
        for check in v["checks"]:
            assert check["key"] and check["label"]
            assert isinstance(check["detail"], str) and check["detail"].strip()
            assert check["help"] == "" or check["help"].strip()


def test_entry_allowed_wrapper_still_returns_a_bool_and_reason():
    assert entry_allowed(GOOD_CONF, GOOD_RISK, 0, 3, True) == (True, "OK")

    ok, reason = entry_allowed(GOOD_CONF, GOOD_RISK, 0, 3, True,
                               blackout=(True, "expiry day"))
    assert ok is False
    assert reason == _verdict(blackout=(True, "expiry day"))["blocking"]
