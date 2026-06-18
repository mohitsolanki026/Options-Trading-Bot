"""P&L sign and level-check correctness for the strategy engine."""
from utils import strategies
from tests.conftest import make_chain, make_summary


def _build(strategy, lots=2, lot_size=75):
    df_oi, options_df = make_chain()
    summary = make_summary()
    return strategies.build_position(
        index="NIFTY", strategy=strategy, summary=summary,
        df_oi=df_oi, options_df=options_df, lots=lots,
        lot_size=lot_size, expiry="26JUN2025",
    )


def _price_map(pos, mult):
    """Return {token: entry_ltp * mult} for every leg."""
    return {leg["token"]: round(leg["entry_ltp"] * mult, 2) for leg in pos["legs"]}


def test_short_straddle_profits_when_premium_falls():
    pos = _build("short_straddle")
    assert len(pos["legs"]) == 2
    assert all(l["action"] == "SELL" for l in pos["legs"])
    assert pos["direction"] == "SELL"
    assert pos["net_credit"] > 0

    # Premium halves → seller profits
    pnl_down = strategies.unrealised_pnl(pos, _price_map(pos, 0.5))
    assert pnl_down > 0
    # Premium doubles → seller loses
    pnl_up = strategies.unrealised_pnl(pos, _price_map(pos, 2.0))
    assert pnl_up < 0


def test_long_ce_profits_when_premium_rises():
    pos = _build("long_ce")
    assert len(pos["legs"]) == 1
    assert pos["legs"][0]["action"] == "BUY"
    assert pos["direction"] == "BUY"

    assert strategies.unrealised_pnl(pos, _price_map(pos, 1.5)) > 0   # CE up → profit
    assert strategies.unrealised_pnl(pos, _price_map(pos, 0.5)) < 0   # CE down → loss


def test_bull_call_spread_has_two_legs_one_each_side():
    pos = _build("bull_call_spread")
    actions = sorted(l["action"] for l in pos["legs"])
    assert actions == ["BUY", "SELL"]
    assert all(l["option_type"] == "CE" for l in pos["legs"])


def test_check_levels_target_and_stop():
    pos = _build("short_straddle")
    # At entry prices, P&L is flat → HOLD
    assert strategies.check_levels(pos, _price_map(pos, 1.0)) == "HOLD"
    # Big premium decay → target
    assert strategies.check_levels(pos, _price_map(pos, 0.1)) == "TARGET"
    # Big premium spike → stop loss
    assert strategies.check_levels(pos, _price_map(pos, 3.0)) == "STOP_LOSS"


def test_realise_pnl_stamps_exit_and_matches_unrealised():
    pos = _build("short_straddle")
    pm = _price_map(pos, 0.5)
    expected = strategies.unrealised_pnl(pos, pm)
    realised = strategies.realise_pnl(pos, pm)
    assert realised == expected
    assert all(l["exit_ltp"] is not None for l in pos["legs"])
    assert pos["exit_combined"] is not None


def test_missing_price_falls_back_to_flat_leg():
    pos = _build("short_straddle")
    # Empty price map → every leg assumed flat → zero P&L
    assert strategies.unrealised_pnl(pos, {}) == 0.0


def test_normalise_strategy_aliases():
    assert strategies.normalise_strategy("Short Straddle") == "short_straddle"
    assert strategies.normalise_strategy("buy ce") == "long_ce"
    assert strategies.normalise_strategy("nonsense xyz") is None
