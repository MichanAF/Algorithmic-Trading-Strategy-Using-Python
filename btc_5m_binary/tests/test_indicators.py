"""Indicator primitives, checked against values computed by hand."""

import numpy as np
import pytest

from btc5m import indicators as ind


def test_sma_matches_manual_mean():
    x = np.arange(1, 11, dtype=float)
    out = ind.sma(x, 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(9.0)


def test_ema_seeds_from_sma_and_tracks_a_constant():
    assert ind.ema(np.full(50, 7.0), 10)[-1] == pytest.approx(7.0)
    x = np.arange(1, 21, dtype=float)
    out = ind.ema(x, 5)
    assert out[4] == pytest.approx(3.0)          # SMA of 1..5
    alpha = 2 / 6
    assert out[5] == pytest.approx(alpha * 6 + (1 - alpha) * 3.0)


def test_rma_uses_wilder_smoothing():
    x = np.arange(1, 11, dtype=float)
    out = ind.rma(x, 4)
    assert out[3] == pytest.approx(2.5)
    assert out[4] == pytest.approx((2.5 * 3 + 5) / 4)


def test_rsi_pins_at_extremes():
    assert ind.rsi(np.arange(1, 40, dtype=float), 14)[-1] == pytest.approx(100.0)
    assert ind.rsi(np.arange(40, 1, -1, dtype=float), 14)[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral_or_undefined():
    out = ind.rsi(np.full(40, 100.0), 14)
    assert not np.isfinite(out[-1]) or 0.0 <= out[-1] <= 100.0


def test_true_range_uses_the_previous_close():
    high = np.array([10.0, 12.0]); low = np.array([9.0, 11.0])
    close = np.array([9.5, 11.5])
    tr = ind.true_range(high, low, close)
    assert tr[0] == pytest.approx(1.0)
    assert tr[1] == pytest.approx(2.5)           # high 12 - prev close 9.5


def test_atr_of_constant_range_equals_that_range():
    n = 60
    close = np.full(n, 100.0)
    assert ind.atr(close + 1, close - 1, close, 14)[-1] == pytest.approx(2.0)


def test_rolling_rank_is_a_percentile_in_zero_one():
    x = np.array([5.0, 1.0, 3.0, 2.0, 4.0])
    out = ind.rolling_rank(x, 5)
    assert out[-1] == pytest.approx(0.8)         # 4 beats 4 of 5 values
    assert ind.rolling_rank(np.arange(50, dtype=float), 10)[-1] == pytest.approx(1.0)


def test_zscore_of_constant_series_is_zero_not_infinite():
    assert ind.zscore(np.full(40, 3.0), 20)[-1] == pytest.approx(0.0)


def test_linreg_slope_and_r2_on_a_perfect_line():
    x = np.arange(30, dtype=float) * 2.0 + 5.0
    assert ind.linreg_slope(x, 10)[-1] == pytest.approx(2.0)
    assert ind.linreg_r2(x, 10)[-1] == pytest.approx(1.0)


def test_linreg_r2_of_noise_is_low():
    rng = np.random.default_rng(0)
    assert ind.linreg_r2(rng.normal(size=400), 20)[100:].mean() < 0.5


def test_adx_rises_in_a_clean_trend():
    n = 120
    close = np.arange(n, dtype=float) + 100.0
    adx, plus_di, minus_di = ind.adx(close + 0.5, close - 0.5, close, 14)
    assert adx[-1] > 50.0
    assert plus_di[-1] > minus_di[-1]


def test_variance_ratio_separates_trend_from_mean_reversion():
    rng = np.random.default_rng(2)
    n = 4000
    trend = np.cumsum(np.abs(rng.normal(1.0, 0.1, n)))          # persistent drift
    flip = np.cumsum(np.where(np.arange(n) % 2 == 0, 1.0, -1.0) * 0.5) + 1000.0
    assert np.nanmean(ind.variance_ratio(1000 + trend, 60, 5)[200:]) > 1.0
    assert np.nanmean(ind.variance_ratio(flip, 60, 5)[200:]) < 1.0


def test_body_dominance_is_signed_and_bounded():
    o = np.array([10.0, 10.0, 10.0]); c = np.array([11.0, 9.0, 10.0])
    h = np.array([11.0, 10.0, 10.5]); l = np.array([10.0, 9.0, 9.5])
    body = ind.body_dominance(o, h, l, c)
    assert body[0] == pytest.approx(1.0)
    assert body[1] == pytest.approx(-1.0)
    assert body[2] == pytest.approx(0.0)
    assert np.all(np.abs(body) <= 1.0)


def test_obv_accumulates_signed_volume():
    close = np.array([10.0, 11.0, 10.0, 12.0])
    volume = np.array([100.0, 200.0, 300.0, 400.0])
    assert ind.obv(close, volume).tolist() == [0.0, 200.0, -100.0, 300.0]


def test_rolling_vwap_of_flat_price_is_that_price():
    n = 40
    p = np.full(n, 50.0)
    assert ind.rolling_vwap(p, p, p, np.full(n, 3.0), 24)[-1] == pytest.approx(50.0)


def test_resample_maps_each_bar_to_the_index_that_closed_it():
    n = 10
    ts = np.arange(n, dtype=np.int64)
    o = np.arange(n, dtype=float)
    idx, _, ro, rh, rl, rc, rv = ind.resample_ohlcv(
        ts, o, o + 1, o - 1, o, np.ones(n), 3)
    assert idx.tolist() == [2, 5, 8]
    assert ro.tolist() == [0.0, 3.0, 6.0]
    assert rc.tolist() == [2.0, 5.0, 8.0]
    assert rh.tolist() == [3.0, 6.0, 9.0]
    assert rl.tolist() == [-1.0, 2.0, 5.0]
    assert rv.tolist() == [3.0, 3.0, 3.0]


def test_short_series_return_all_nan_rather_than_raising():
    x = np.arange(3, dtype=float)
    for fn in (ind.sma, ind.ema, ind.rma, ind.rolling_std, ind.rolling_rank):
        assert np.isnan(fn(x, 10)).all()


def test_window_must_be_positive():
    with pytest.raises(ValueError):
        ind.sma(np.arange(10, dtype=float), 0)


def test_two_dimensional_input_is_rejected():
    with pytest.raises(ValueError):
        ind.sma(np.ones((4, 4)), 2)
