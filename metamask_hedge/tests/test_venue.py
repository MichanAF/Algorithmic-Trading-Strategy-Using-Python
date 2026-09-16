"""The venue's numbers, and the arithmetic the strategy hangs off them."""

import pytest

from mmhedge.venue import LARGE, MAJOR, METAMASK


def test_liquidation_move_matches_closed_form():
    # r = (1/L - m) / (1 + m).  These are the numbers the README quotes, and a
    # regression here silently changes every sizing decision downstream.
    assert MAJOR.liquidation_move(2.0) == pytest.approx(0.485, abs=5e-4)
    assert MAJOR.liquidation_move(3.0) == pytest.approx(0.320, abs=5e-4)
    assert MAJOR.liquidation_move(10.0) == pytest.approx(0.0891, abs=5e-4)
    assert MAJOR.liquidation_move(50.0) == pytest.approx(0.0099, abs=5e-4)


def test_higher_maintenance_liquidates_sooner():
    assert LARGE.liquidation_move(3.0) < MAJOR.liquidation_move(3.0)


def test_leverage_and_liquidation_are_inverses():
    for move in (0.20, 0.35, 0.50, 0.75):
        lev = MAJOR.leverage_for_survival(move)
        assert MAJOR.liquidation_move(lev) == pytest.approx(move, abs=1e-9)


def test_surviving_fifty_percent_needs_about_two_x():
    assert MAJOR.leverage_for_survival(0.50) == pytest.approx(1.942, abs=1e-3)
    assert MAJOR.margin_fraction_for_survival(0.50) == pytest.approx(0.515, abs=1e-3)


def test_rejects_nonsense_leverage():
    with pytest.raises(ValueError):
        MAJOR.liquidation_move(0.0)
    with pytest.raises(ValueError):
        MAJOR.leverage_for_survival(-0.1)


def test_spot_book_is_far_more_expensive_than_the_perp_book():
    # The whole architecture rests on this ratio. If it ever drops near 1 the
    # strategy should be rebalancing with swaps instead.
    assert METAMASK.rebalance_cost_ratio() > 20.0
    assert METAMASK.spot_cost_pct(round_trip=True) > 0.015
    assert METAMASK.perp_cost_pct(round_trip=True) < 0.001


def test_maker_round_trip_is_a_rebate_not_a_cost():
    assert METAMASK.perp_cost_pct(maker=True, round_trip=True) < 0.0
    assert METAMASK.perp_cost_pct(maker=False, round_trip=True) > 0.0


def test_withdrawal_fee_dominates_small_positions():
    small = METAMASK.close_hedge_cost_pct(maker=False, withdraw_notional=500.0)
    large = METAMASK.close_hedge_cost_pct(maker=False, withdraw_notional=50_000.0)
    assert small > large
    assert small > 0.002


def test_funding_formula_clamps_and_caps():
    v = METAMASK
    # With premium at the interest rate, funding equals the interest rate.
    assert v.funding_rate(v.funding_interest_per_hour) == pytest.approx(
        v.funding_interest_per_hour)
    # A huge premium is only adjusted by the clamp, not erased by it.
    big = 0.01
    assert v.funding_rate(big) == pytest.approx(big - v.funding_clamp)
    # And nothing escapes the venue cap.
    assert v.funding_rate(100.0) == v.funding_cap_per_hour
    assert v.funding_rate(-100.0) == -v.funding_cap_per_hour


def test_baseline_funding_is_about_eleven_percent():
    assert METAMASK.baseline_funding_apr() == pytest.approx(0.1095, abs=1e-3)


def test_tier_lookup_falls_back_to_the_safe_side():
    assert METAMASK.tier_for("BTC").name == "major"
    assert METAMASK.tier_for("sol").name == "large"
    assert METAMASK.tier_for("SOMETHING_NEW").name == "long_tail"


def test_staking_fee_share_is_applied():
    assert METAMASK.net_staking_apr(0.04) == pytest.approx(0.034)


def test_overrides_do_not_mutate_the_shared_default():
    cheap = METAMASK.with_overrides(swap_fee_pct=0.0)
    assert cheap.swap_fee_pct == 0.0
    assert METAMASK.swap_fee_pct > 0.0


def test_non_crypto_tier_is_flagged_unverified():
    # It is a guess. If someone confirms the real bracket, the flag should
    # change in the same commit as the number.
    assert METAMASK.tiers["non_crypto"].verified is False
    assert all(METAMASK.tiers[t].verified for t in ("major", "large", "long_tail"))
