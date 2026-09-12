"""Bar containers, CSV handling, and the synthetic generator's realism."""

import numpy as np
import pytest

from btc5m.data import BAR_SECONDS, BarSeries, load_csv, synthetic


def test_series_validates_shape_and_ordering():
    n = 10
    ts = np.arange(n, dtype=np.int64) * BAR_SECONDS
    ones = np.ones(n)
    BarSeries(ts=ts, open=ones, high=ones, low=ones, close=ones, volume=ones)

    with pytest.raises(ValueError, match="ragged"):
        BarSeries(ts=ts, open=ones[:-1], high=ones, low=ones, close=ones, volume=ones)
    with pytest.raises(ValueError, match="strictly increasing"):
        BarSeries(ts=ts[::-1].copy(), open=ones, high=ones, low=ones,
                  close=ones, volume=ones)
    with pytest.raises(ValueError, match="spread_bps"):
        BarSeries(ts=ts, open=ones, high=ones, low=ones, close=ones, volume=ones,
                  spread_bps=ones[:-1])


def test_slicing_keeps_every_column_aligned():
    s = synthetic(500, seed=3)
    part = s[100:200]
    assert len(part) == 100
    assert part.close[0] == pytest.approx(s.close[100])
    assert part.ts[-1] == s.ts[199]
    assert part.symbol == s.symbol
    assert len(s.tail(50)) == 50


def test_slicing_with_a_non_slice_is_rejected():
    with pytest.raises(TypeError):
        synthetic(500)[3]


def test_csv_round_trip_preserves_prices(tmp_path):
    original = synthetic(800, seed=44)
    path = original.write_csv(tmp_path / "bars.csv")
    restored = load_csv(path)
    assert len(restored) == len(original)
    assert np.allclose(restored.close, original.close, atol=0.01)
    assert np.array_equal(restored.ts, original.ts)


def test_csv_accepts_common_column_spellings(tmp_path):
    p = tmp_path / "alt.csv"
    p.write_text("Date,Open,High,Low,Adj Close,Volume\n"
                 "2024-01-01T00:05:00Z,1,2,0.5,1.5,10\n"
                 "2024-01-01T00:10:00Z,1.5,2.5,1.0,2.0,20\n")
    s = load_csv(p)
    assert len(s) == 2
    assert s.close.tolist() == [1.5, 2.0]
    assert s.volume.tolist() == [10.0, 20.0]


def test_csv_fills_in_missing_optional_columns(tmp_path):
    p = tmp_path / "close_only.csv"
    p.write_text("timestamp,close\n0,100\n300,101\n600,102\n")
    s = load_csv(p)
    assert s.high.tolist() == [100.0, 101.0, 102.0]
    assert s.open.tolist() == [100.0, 100.0, 101.0]
    assert s.volume.tolist() == [1.0, 1.0, 1.0]


def test_csv_parses_second_and_millisecond_timestamps(tmp_path):
    p = tmp_path / "ms.csv"
    p.write_text("timestamp,close\n1700000000000,100\n1700000300000,101\n")
    s = load_csv(p)
    assert s.ts.tolist() == [1_700_000_000, 1_700_000_300]


def test_csv_errors_are_specific(tmp_path):
    empty = tmp_path / "e.csv"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_csv(empty)

    no_close = tmp_path / "n.csv"
    no_close.write_text("alpha,beta\n1,2\n")
    with pytest.raises(ValueError, match="close column"):
        load_csv(no_close)

    header_only = tmp_path / "h.csv"
    header_only.write_text("timestamp,close\n")
    with pytest.raises(ValueError, match="no data rows"):
        load_csv(header_only)

    bad = tmp_path / "b.csv"
    bad.write_text("timestamp,close\n0,not_a_number\n")
    with pytest.raises(ValueError, match="bad close value"):
        load_csv(bad)


def test_csv_skips_blank_lines(tmp_path):
    p = tmp_path / "blanks.csv"
    p.write_text("timestamp,close\n0,100\n\n300,101\n")
    assert len(load_csv(p)) == 2


# --------------------------------------------------------------------------- #
# synthetic generator
# --------------------------------------------------------------------------- #

def test_synthetic_is_deterministic_for_a_seed():
    assert np.array_equal(synthetic(500, seed=9).close, synthetic(500, seed=9).close)
    assert not np.array_equal(synthetic(500, seed=9).close,
                              synthetic(500, seed=10).close)


def test_synthetic_bars_are_internally_consistent():
    s = synthetic(3000, seed=12)
    assert np.all(s.high >= np.maximum(s.open, s.close) - 1e-9)
    assert np.all(s.low <= np.minimum(s.open, s.close) + 1e-9)
    assert np.all(s.volume > 0)
    assert np.all(s.close > 0)
    assert np.all(np.diff(s.ts) == BAR_SECONDS)


def test_synthetic_volatility_is_in_a_plausible_range_for_btc():
    returns = np.diff(np.log(synthetic(40_000, seed=13).close))
    annualised = returns.std() * np.sqrt(365 * 288)
    assert 0.15 < annualised < 1.2, f"implausible annual vol {annualised:.2f}"


def test_synthetic_autocorrelation_is_near_what_btc_shows():
    """Defends the fixture: a rigged one would flatter or condemn the stack."""
    r = np.diff(np.log(synthetic(120_000, seed=11).close))
    lag1 = np.corrcoef(r[:-1], r[1:])[0, 1]
    assert -0.06 < lag1 < 0.0, f"lag-1 autocorrelation {lag1:.4f} is unrealistic"


def test_synthetic_direction_is_close_to_a_coin_flip():
    up_share = np.mean(np.diff(synthetic(60_000, seed=14).close) > 0)
    assert 0.45 < up_share < 0.55


def test_regime_parameters_do_what_they_say():
    trendy = np.diff(np.log(synthetic(40_000, seed=15, trend_ar=0.4,
                                      chop_ar=0.4).close))
    choppy = np.diff(np.log(synthetic(40_000, seed=15, trend_ar=-0.4,
                                      chop_ar=-0.4).close))
    assert np.corrcoef(trendy[:-1], trendy[1:])[0, 1] > 0.2
    assert np.corrcoef(choppy[:-1], choppy[1:])[0, 1] < -0.2


def test_time_at_returns_utc():
    s = synthetic(10, seed=1, start_ts=1_699_920_000)
    assert s.time_at(0).strftime("%Y-%m-%d %H:%M") == "2023-11-14 00:00"
    assert s.time_at(1).strftime("%H:%M") == "00:05"
