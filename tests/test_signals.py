"""Signals must only fire on genuine edge, and confluence must require conviction."""
from utils import signal_engine as se


# ── individual signals only score on edge ──

def test_vix_only_scores_at_extremes():
    assert se.signal_vix(15)["score"] == 0          # normal → no edge (was always +1)
    assert se.signal_vix(10)["bias"] == "SELL_PREMIUM"
    assert se.signal_vix(25)["bias"] == "BUY_OPTIONS"


def test_oi_position_zero_in_midrange():
    assert se.signal_oi_position(24000, 24200, 24100)["score"] == 0   # mid → no edge
    assert se.signal_oi_position(24000, 24200, 24010)["bias"] == "BULLISH"   # near support
    assert se.signal_oi_position(24000, 24200, 24190)["bias"] == "BEARISH"   # near resistance


def test_pcr_single_signal_with_weight():
    assert se.signal_pcr(1.0)["score"] == 0
    assert se.signal_pcr(1.6)["score"] == 2 and se.signal_pcr(1.6)["bias"] == "BULLISH"
    assert se.signal_pcr(0.5)["score"] == 2 and se.signal_pcr(0.5)["bias"] == "BEARISH"


def test_iv_rank_signal_uses_real_rank():
    assert se.signal_iv_rank(None)["score"] == 0          # unknown → no edge
    assert se.signal_iv_rank(70)["bias"] == "SELL_PREMIUM"
    assert se.signal_iv_rank(20)["bias"] == "BUY_OPTIONS"
    assert se.signal_iv_rank(45)["score"] == 0            # mid → no edge


# ── confluence requires score AND confirmed bias ──

def _conf(**kw):
    base = dict(pcr=1.0, support=24000, resistance=24200, nifty_spot=24100,
                vix=15, days_to_expiry=5, regime="SIDEWAYS", iv_rank=None, ta={})
    base.update(kw)
    return se.run_confluence(**base)


def test_sell_premium_setup_confirmed():
    c = _conf(iv_rank=70, vix=10, days_to_expiry=1)   # IVR2 + VIX1 + theta1 = 4
    assert c["overall_bias"] == "SELL_PREMIUM"
    assert c["bias_confirmed"] is True
    assert c["premium_sell_ok"] is True
    assert c["score"] >= c["threshold"]


def test_directional_setup_confirmed():
    ta = {"bull_count": 3, "overall": "BULLISH", "rsi": 62}
    c = _conf(pcr=1.6, nifty_spot=24010, ta=ta)        # PCR2 + OI(support)1 + TA2 = 5
    assert c["overall_bias"] == "BULLISH"
    assert c["bias_confirmed"] is True


def test_muddled_setup_not_confirmed():
    c = _conf(pcr=1.3, nifty_spot=24190)               # bull 1 vs bear 1 → margin 0
    assert c["bias_confirmed"] is False
    assert c["decision"].endswith("SKIP")


def test_low_iv_rank_blocks_premium_edge():
    c = _conf(iv_rank=20, vix=10, days_to_expiry=1)    # IVR says BUY, VIX/theta say SELL
    # premium_sell_ok must be False when IV rank is cheap
    assert c["premium_sell_ok"] is False
