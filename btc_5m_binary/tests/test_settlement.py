"""Which price decides the bet. No network: every window is hand-built so that a
named rule disagrees with close-to-close for a stated reason.

The four rules are readings of one sentence in a live Polymarket market and of
the 60-second TWAP feed it names. They agree on a decisive window and disagree on
a close one, so the fixtures here are close windows: that is the population a
five-minute strategy lives in, and the only one where the reading matters.
"""

import numpy as np
import pytest

from btc5m.data import BarSeries
from btc5m.gates import DOWN, UP
from btc5m.settlement import (RULE_NAMES, WindowPrices, compare, hit_rates,
                              outcomes, render_comparison, render_hit_rates,
                              render_venue_scores, score_against_venue, typical,
                              window_prices)

START = 1_789_481_100          # a five-minute boundary


def minutes(*bars, start: int = START) -> BarSeries:
    """Minute bars from (high, low, close) triples, one a minute from ``start``.

    The first triple is therefore the minute *closing* at the window's start --
    the one holding the opening price and the pre-window average -- and the next
    five are the minutes inside the window.
    """
    ts = [start + 60 * k for k in range(len(bars))]
    return BarSeries(ts=ts, open=[c for _, _, c in bars],
                     high=[h for h, _, _ in bars], low=[l for _, l, _ in bars],
                     close=[c for _, _, c in bars], volume=[1.0] * len(bars))


def flat(price: float) -> tuple[float, float, float]:
    """A minute whose typical price and close are both ``price``."""
    return (price, price, price)


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def test_a_window_needs_its_own_minute_and_the_five_inside_it():
    series = minutes(flat(100), flat(101), flat(102), flat(103), flat(104), flat(105))
    prices = window_prices(series)
    assert len(prices) == 1
    assert int(prices.starts[0]) == START
    assert prices.open_price[0] == 100          # close of the minute ending at the start
    assert prices.close_price[0] == 105         # close of the last minute inside
    assert prices.end_twap[0] == 105
    assert prices.window_twap[0] == pytest.approx(103.0)   # mean of 101..105
    assert prices.pre_twap[0] == 100


def test_a_missing_minute_drops_its_window_rather_than_changing_the_outcome():
    series = minutes(*[flat(100 + k) for k in range(11)])
    # Remove the third minute of the first window.
    keep = [i for i, t in enumerate(series.ts) if int(t) != START + 180]
    gapped = BarSeries(ts=series.ts[keep], open=series.open[keep],
                       high=series.high[keep], low=series.low[keep],
                       close=series.close[keep], volume=series.volume[keep])
    starts = [int(s) for s in window_prices(gapped).starts]
    assert START not in starts
    assert START + 300 in starts


def test_a_series_that_is_not_minute_bars_is_refused():
    five = BarSeries(ts=[START + 300 * k for k in range(10)], open=[1.0] * 10,
                     high=[1.0] * 10, low=[1.0] * 10, close=[1.0] * 10,
                     volume=[1.0] * 10)
    with pytest.raises(ValueError, match="expected minute bars"):
        window_prices(five)


def test_a_series_too_short_or_unaligned_yields_nothing():
    assert len(window_prices(minutes(flat(100), flat(101)))) == 0
    unaligned = minutes(*[flat(100)] * 8, start=START + 7)
    assert len(window_prices(unaligned)) == 0


def test_a_nan_price_drops_its_window():
    series = minutes(*[flat(100 + k) for k in range(6)])
    series.close[3] = np.nan
    assert len(window_prices(series)) == 0


def test_typical_price_is_the_bars_stand_in_for_its_average():
    series = minutes((102.0, 99.0, 100.5))
    assert typical(series)[0] == pytest.approx((102.0 + 99.0 + 100.5) / 3.0)


# --------------------------------------------------------------------------- #
# the rules disagree, one at a time
# --------------------------------------------------------------------------- #


def test_a_window_that_dips_and_recovers_splits_the_average_from_the_close():
    """Close above the open, but the average of the window well below it: the
    literal reading of the market's text pays DOWN where the backtest pays UP."""
    series = minutes(flat(100),                       # the minute ending at the open
                     flat(98), flat(97), flat(97), flat(98),
                     (100.4, 100.2, 100.5))           # ends strong: typical 100.37
    sides = outcomes(window_prices(series))
    assert sides["close_to_close"][0] == UP           # 100.5 >= 100
    assert sides["twap_window"][0] == DOWN            # mean 98.07 < 100
    assert sides["twap60_vs_open"][0] == UP           # 100.37 >= 100
    assert sides["twap60_ends"][0] == UP              # 100.37 >= 100


def test_a_last_minute_that_closes_strong_from_low_splits_the_twap_from_the_close():
    """The last minute spends most of itself below the open and closes just above
    it. Close-to-close pays UP; a 60-second average of that minute pays DOWN."""
    series = minutes(flat(100),
                     flat(100), flat(100), flat(100), flat(100),
                     (101.0, 98.0, 100.5))            # typical 99.83
    sides = outcomes(window_prices(series))
    assert sides["close_to_close"][0] == UP           # 100.5 >= 100
    assert sides["twap60_vs_open"][0] == DOWN         # 99.83 < 100
    assert sides["twap60_ends"][0] == DOWN            # 99.83 < 100.0
    assert sides["twap_window"][0] == DOWN            # mean 99.97 < 100


def test_the_two_ends_rule_can_differ_from_the_open_rule():
    """Reading a 60-second stream at both ends compares averages; comparing the
    end average to the opening *price* is a different bet, and a window whose
    first minute is stretched separates them."""
    series = minutes((104.0, 96.0, 100.0),            # pre-window typical 100.0
                     flat(99), flat(99), flat(99), flat(99),
                     (99.9, 99.5, 99.8))              # typical 99.73
    sides = outcomes(window_prices(series))
    assert sides["twap60_ends"][0] == DOWN            # 99.73 < 100.0
    assert sides["twap60_vs_open"][0] == DOWN         # 99.73 < 100
    # Now lift the pre-window typical below the end typical while the open holds.
    series = minutes((99.0, 99.0, 100.0),             # typical 99.33, close 100
                     flat(99), flat(99), flat(99), flat(99),
                     (99.9, 99.5, 99.8))              # typical 99.73
    sides = outcomes(window_prices(series))
    assert sides["twap60_ends"][0] == UP              # 99.73 >= 99.33
    assert sides["twap60_vs_open"][0] == DOWN         # 99.73 < 100


def test_an_exact_tie_pays_up_because_the_venue_resolves_on_greater_or_equal():
    series = minutes(flat(100), flat(100), flat(100), flat(100), flat(100), flat(100))
    for name, side in outcomes(window_prices(series)).items():
        assert side[0] == UP, name


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #


def two_windows() -> BarSeries:
    """One window where the average disagrees with the close, one where it does not."""
    return minutes(flat(100),
                   flat(98), flat(97), flat(97), flat(98), (100.4, 100.2, 100.5),
                   flat(101), flat(102), flat(103), flat(104), flat(105))


def test_the_comparison_counts_where_each_rule_pays_the_other_side():
    prices = window_prices(two_windows())
    assert len(prices) == 2
    rows = {row.rule: row for row in compare(prices)}
    assert rows["close_to_close"].differs == 0        # it is the baseline
    assert rows["twap_window"].differs == 1
    assert rows["twap_window"].windows == 2
    assert rows["twap_window"].rate == pytest.approx(0.5)
    assert rows["close_to_close"].up_share == pytest.approx(1.0)


def test_the_comparison_can_restrict_itself_to_the_windows_actually_bet():
    prices = window_prices(two_windows())
    rows = {row.rule: row for row in compare(prices, bet_starts=[START + 300])}
    # The second window is the one bet, and every rule agrees there.
    assert rows["twap_window"].bet_windows == 1
    assert rows["twap_window"].differs_on_bet == 0
    assert rows["twap_window"].differs == 1          # still counted overall
    rows = {row.rule: row for row in compare(prices, bet_starts=[START])}
    assert rows["twap_window"].differs_on_bet == 1
    assert rows["twap_window"].bet_rate == pytest.approx(1.0)


def test_a_bet_window_the_minute_bars_do_not_cover_is_simply_absent():
    prices = window_prices(two_windows())
    rows = {row.rule: row for row in compare(prices, bet_starts=[START - 6000])}
    assert rows["twap_window"].bet_windows == 0
    assert np.isnan(rows["twap_window"].bet_rate)


def test_the_comparison_renders_with_its_caveat():
    text = render_comparison(compare(window_prices(two_windows())))
    assert "baseline: close_to_close" in text
    assert "windows:  2" in text
    assert "twap_window" in text and "50.00%" in text
    assert "A minute bar is not a TWAP" in text
    assert "A disagreement rate is not a loss" in text


# --------------------------------------------------------------------------- #
# what the strategy would have been paid
# --------------------------------------------------------------------------- #


def test_the_hit_rate_is_scored_against_the_side_the_strategy_took():
    """The measurement that matters. On the dip-and-recover window a DOWN bet is
    paid by the average-over-the-window rule and refused by close-to-close; on
    the rising window a DOWN bet loses under every rule."""
    prices = window_prices(two_windows())
    rows = {row.rule: row for row in hit_rates(prices, {START: DOWN, START + 300: DOWN})}
    assert rows["close_to_close"].bets == 2 and rows["close_to_close"].wins == 0
    assert rows["twap_window"].wins == 1
    assert rows["twap_window"].rate == pytest.approx(0.5)
    # The same windows, bet UP: exactly the mirror image.
    rows = {row.rule: row for row in hit_rates(prices, {START: UP, START + 300: UP})}
    assert rows["close_to_close"].wins == 2
    assert rows["twap_window"].wins == 1


def test_a_bet_outside_the_minute_bars_is_not_scored():
    prices = window_prices(two_windows())
    rows = hit_rates(prices, {START - 6000: UP})
    assert all(r.bets == 0 for r in rows)
    assert "no bet window falls inside these minute bars" in render_hit_rates(rows)


def test_the_hit_rate_table_names_the_spread_and_what_it_means():
    prices = window_prices(two_windows())
    text = render_hit_rates(hit_rates(prices, {START: DOWN, START + 300: DOWN}),
                            break_even=0.5175)
    assert "bets scored: 2" in text
    assert "close_to_close      0.00%" in text
    assert "twap_window        50.00%" in text
    assert "(+50.00 vs close-to-close)" in text
    assert "differ by up to 50.00 points" in text
    assert "chosen against close_to_close" in text
    assert "-51.75%" in text                       # against break-even


def test_the_disagreement_caveat_states_the_arithmetic_rather_than_a_slogan():
    """A 5% flip rate costs a third of a point if flips are independent of the
    bet, not ten points. The text has to say which case it is describing."""
    text = render_comparison(compare(window_prices(two_windows())))
    assert "p - f(2p - 1)" in text
    assert "21% flip rate to" in text
    assert "malign case" in text
    assert "hit-rate table below is" in text


# --------------------------------------------------------------------------- #
# which rule does the venue use?
# --------------------------------------------------------------------------- #


def test_the_venue_outcome_picks_out_the_rule_that_matches_it():
    prices = window_prices(two_windows())
    # The venue paid DOWN on the dip-and-recover window: only twap_window agrees.
    scores, informative = score_against_venue(prices, {START: "Down", START + 300: "Up"})
    by_rule = {s.rule: s for s in scores}
    assert informative == 1                      # the rules split on one window
    assert by_rule["twap_window"].matched == 2
    assert by_rule["close_to_close"].matched == 1
    assert by_rule["twap_window"].rate == pytest.approx(1.0)
    text = render_venue_scores(scores, informative)
    assert "settled windows matched to minute bars: 2" in text
    assert "windows where the rules disagree: 1" in text
    assert "twap_window" in text and "100.0%" in text


def test_windows_the_rules_all_agree_on_cannot_tell_them_apart():
    prices = window_prices(two_windows())
    scores, informative = score_against_venue(prices, {START + 300: "Up"})
    assert informative == 0
    assert all(s.matched == 1 for s in scores)
    text = render_venue_scores(scores, informative)
    assert "cannot yet tell them apart" in text


def test_a_settled_window_with_no_minute_bars_is_skipped():
    prices = window_prices(two_windows())
    scores, informative = score_against_venue(prices, {START - 6000: "Up"})
    assert all(s.checked == 0 for s in scores)
    assert "nothing to score yet" in render_venue_scores(scores, informative)


def test_every_rule_is_scored_and_named():
    prices = window_prices(two_windows())
    scores, _ = score_against_venue(prices, {START: "Down"})
    assert [s.rule for s in scores] == list(RULE_NAMES)
