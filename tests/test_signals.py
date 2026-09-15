"""Signals must only fire on genuine edge, and confluence must require conviction."""
from utils import signal_engine as se


# ── individual signals only score on edge ──

def test_vix_scores_on_its_own_history_not_its_level():
    assert se.signal_vix(15)["score"] == 0                          # ordinary → no edge
    assert se.signal_vix(10)["score"] == 0                          # calm alone is not an edge
    assert se.signal_vix(25)["bias"] == "BUY_OPTIONS"               # outright fear → protect
    assert se.signal_vix(11, vix_rank=80)["bias"] == "SELL_PREMIUM" # dear for its range
    assert se.signal_vix(11, vix_rank=10)["bias"] == "BUY_OPTIONS"  # cheap for its range
    assert se.signal_vix(11, vix_rank=45)["score"] == 0


def test_theta_rewards_a_holdable_expiry_not_the_last_days():
    assert se.signal_theta_expiry(1)["score"] == 0     # gamma trap
    assert se.signal_theta_expiry(2)["score"] == 0
    assert se.signal_theta_expiry(5)["score"] == 2     # sweet spot
    assert se.signal_theta_expiry(20)["score"] == 1    # monthly, slow but steady
    assert se.signal_theta_expiry(60)["score"] == 0


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
    c = _conf(iv_rank=70, days_to_expiry=5)           # IVR2 + theta2 = 4
    assert c["overall_bias"] == "SELL_PREMIUM"
    assert c["bias_confirmed"] is True
    assert c["premium_sell_ok"] is True
    assert c["score"] >= c["threshold"]


def test_theta_does_not_vote_and_only_counts_for_selling_premium():
    # A good expiry date alone must not confirm a view...
    c = _conf(days_to_expiry=5)
    assert c["bias_confirmed"] is False and c["score"] == 0
    # ...nor pad a thin directional setup over the line.
    c = _conf(pcr=1.6, days_to_expiry=5)              # PCR 2 alone; theta not counted
    assert c["overall_bias"] == "BULLISH" and c["score"] == 2


def test_vix_rank_not_double_counted_while_it_is_the_iv_proxy():
    proxy = _conf(iv_rank=70, vix_rank=70, iv_rank_source="vix", days_to_expiry=5)
    own   = _conf(iv_rank=70, vix_rank=70, iv_rank_source="index", days_to_expiry=5)
    assert proxy["score"] == 4 and own["score"] == 5


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
