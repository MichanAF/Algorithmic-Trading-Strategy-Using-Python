"""Hold duration: the arithmetic that decides it."""

import pytest

from mmhedge.carry import (INF, break_even_table, funding_apr_needed,
                           quote_carry, realised_funding)
from mmhedge.venue import METAMASK


def test_building_from_cash_is_vastly_slower_to_break_even():
    overlay = quote_carry(METAMASK, 0.11, leverage=2.0, from_cash=False, maker=False)
    scratch = quote_carry(METAMASK, 0.11, leverage=2.0, from_cash=True, maker=False)
    # This ratio is the entire argument for an overlay over a from-cash trade.
    assert overlay.break_even_days < 5.0
    assert scratch.break_even_days > 100.0
    assert scratch.break_even_days / overlay.break_even_days > 20.0


def test_maker_fills_break_even_immediately():
    q = quote_carry(METAMASK, 0.11, leverage=2.0, maker=True)
    assert q.cost_frac < 0.0
    assert q.break_even_days == 0.0


def test_funding_below_the_cash_alternative_never_breaks_even():
    q = quote_carry(METAMASK, 0.005, leverage=2.0, from_cash=True, maker=False)
    assert q.break_even_days == INF
    assert q.min_hold_days == INF
    assert not q.worth_it(3650)


def test_minimum_hold_is_a_multiple_of_break_even():
    q = quote_carry(METAMASK, 0.11, leverage=2.0, maker=False, safety_multiple=3.0)
    assert q.min_hold_days == pytest.approx(q.break_even_days * 3.0)


def test_floor_days_applies_when_break_even_is_trivial():
    q = quote_carry(METAMASK, 0.60, leverage=2.0, maker=True, floor_days=2.0)
    assert q.min_hold_days == 2.0


def test_pnl_crosses_zero_at_break_even():
    q = quote_carry(METAMASK, 0.25, leverage=2.0, maker=False)
    be = q.break_even_days
    # pnl_frac ignores opportunity cost, so it turns positive at or before the
    # mUSD-adjusted break-even; either way the sign must flip across it.
    assert q.pnl_frac(be * 0.2) < q.pnl_frac(be * 5.0)
    assert q.pnl_frac(be * 5.0) > 0.0


def test_richer_funding_shortens_the_hold():
    prev = None
    for apr in (0.11, 0.20, 0.35, 0.60):
        q = quote_carry(METAMASK, apr, leverage=2.0, maker=False)
        if prev is not None:
            assert q.break_even_days < prev
        prev = q.break_even_days


def test_funding_apr_needed_inverts_break_even():
    for days in (3.0, 7.0, 30.0):
        apr = funding_apr_needed(METAMASK, days, leverage=2.0, maker=False)
        q = quote_carry(METAMASK, apr, leverage=2.0, maker=False)
        assert q.break_even_days == pytest.approx(days, rel=1e-6)


def test_from_cash_needs_far_richer_funding_for_the_same_hold():
    overlay = funding_apr_needed(METAMASK, 30.0, leverage=2.0, maker=False)
    scratch = funding_apr_needed(METAMASK, 30.0, leverage=2.0, from_cash=True,
                                 maker=False)
    assert scratch > overlay * 5.0


def test_withdrawal_fee_raises_the_bar_on_small_notionals():
    plain = quote_carry(METAMASK, 0.20, leverage=2.0, maker=False)
    tiny = quote_carry(METAMASK, 0.20, leverage=2.0, maker=False,
                       notional_usd=400.0, withdraw=True)
    assert tiny.cost_frac > plain.cost_frac
    assert tiny.break_even_days > plain.break_even_days


def test_realised_funding_is_a_plain_sum_on_notional():
    rates = [0.0000125] * 24
    assert realised_funding(rates, 10_000.0) == pytest.approx(3.0)


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        quote_carry(METAMASK, 0.2, leverage=0.0)
    with pytest.raises(ValueError):
        funding_apr_needed(METAMASK, 0.0, leverage=2.0)


def test_break_even_table_renders_both_verdicts():
    text = break_even_table(METAMASK, leverage=2.0)
    assert "immediate" in text
    assert "never" in text
