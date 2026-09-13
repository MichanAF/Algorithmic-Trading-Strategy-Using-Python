"""Bar containers, CSV handling, paged history, and the generator's realism."""

import json
from datetime import datetime, timezone

import numpy as np
import pytest

from btc5m import data
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


# --------------------------------------------------------------------------- #
# paged history: the walk, without a network
# --------------------------------------------------------------------------- #

class _FakeBinance:
    """A Binance klines endpoint holding a finite history.

    Honours endTime and the 1000-row cap the way the real one does, so the
    paging walk is exercised rather than mocked away.
    """

    def __init__(self, bars: int, last_close: int, page: int = 1000):
        self.page, self.calls = page, []
        self.rows = {}
        for i in range(bars):
            close = last_close - i * 300
            self.rows[close] = [
                (close - 300) * 1000, "100.0", "101.0", "99.0", "100.5",
                "12.0", close * 1000 - 1]      # closeTime: last ms of the bar

    def __call__(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.calls.append(url)
        end_ms = int(url.split("endTime=")[1].split("&")[0])
        end_s = end_ms // 1000
        keys = sorted(k for k in self.rows if k <= end_s)[-self.page:]
        return _Response(json.dumps([self.rows[k] for k in keys]))


class _Response:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_history_pages_backwards_until_it_has_enough(monkeypatch):
    """One request caps at 1000 bars; a year needs 106 of them stitched."""
    last = 1_757_675_700
    fake = _FakeBinance(bars=5_000, last_close=last)
    monkeypatch.setattr(data.urllib.request, "urlopen", fake)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    series = data.fetch_history("binance", "BTCUSDT", bars=2_500,
                                pause_seconds=0)
    assert len(series) == 2_500
    assert len(fake.calls) == 3                  # 1000 + 1000 + 500 of a page
    # Ascending, contiguous, and ending on the newest closed bar.
    assert series.ts[-1] == last
    assert list(np.diff(series.ts)) == [300] * 2_499


def test_history_stops_when_the_exchange_runs_out(monkeypatch):
    """A symbol younger than the request yields what exists, not a loop."""
    fake = _FakeBinance(bars=1_200, last_close=1_757_675_700)
    monkeypatch.setattr(data.urllib.request, "urlopen", fake)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    series = data.fetch_history("binance", bars=105_120, pause_seconds=0)
    assert len(series) == 1_200
    assert len(fake.calls) == 3        # third page returns nothing new, so stop


def test_history_never_repeats_a_page(monkeypatch):
    """The cursor steps past the oldest bar held, so pages cannot overlap."""
    fake = _FakeBinance(bars=3_000, last_close=1_757_675_700)
    monkeypatch.setattr(data.urllib.request, "urlopen", fake)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    data.fetch_history("binance", bars=3_000, pause_seconds=0)
    ends = [int(u.split("endTime=")[1].split("&")[0]) for u in fake.calls]
    assert ends == sorted(ends, reverse=True)
    assert len(set(ends)) == len(ends)


def test_history_drops_a_candle_that_has_not_closed(monkeypatch):
    """Betting on an unclosed bar is how a 5-minute backtest becomes fiction."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    future = now + 600                      # a bar closing ten minutes from now
    fake = _FakeBinance(bars=50, last_close=future)
    monkeypatch.setattr(data.urllib.request, "urlopen", fake)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    series = data.fetch_history("binance", bars=50, pause_seconds=0)
    assert series.ts[-1] <= now


def test_history_refuses_kraken_with_the_reason():
    """Its public OHLC endpoint serves ~720 candles whatever you ask for."""
    with pytest.raises(ValueError, match="720"):
        data.fetch_history("kraken", bars=105_120)


def test_history_rejects_a_nonsense_bar_count():
    with pytest.raises(ValueError, match="must be positive"):
        data.fetch_history("binance", bars=0)


def test_history_says_how_far_it_got_before_the_network_died(monkeypatch):
    """A partial download is worth naming; a total failure gets the remedy."""
    fake = _FakeBinance(bars=5_000, last_close=1_757_675_700)
    state = {"n": 0}

    def flaky(request, timeout=None):
        state["n"] += 1
        if state["n"] > 2:
            raise data.urllib.error.URLError("connection reset")
        return fake(request, timeout=timeout)

    monkeypatch.setattr(data.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(data.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="stopped responding after 2000"):
        data.fetch_history("binance", bars=5_000, pause_seconds=0)


def test_history_reports_an_unreachable_exchange_with_the_workaround(monkeypatch):
    def dead(request, timeout=None):
        raise data.urllib.error.URLError("blocked")

    monkeypatch.setattr(data.urllib.request, "urlopen", dead)
    with pytest.raises(RuntimeError, match="geo-blocked"):
        data.fetch_history("binance", bars=2_000, pause_seconds=0)


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #

def test_binance_bars_land_on_the_five_minute_boundary():
    """closeTime is the last millisecond of the bar, so //1000 is a second short.
    A one-second skew matters: predict.fun windows sit on exact 300s grids."""
    open_ms = 1_757_675_400 * 1000
    kline = [open_ms, "1", "2", "0.5", "1.5", "10", open_ms + 299_999]
    rows = data._parse_candles("binance", [kline], "BTCUSDT")
    assert rows[0][0] == 1_757_675_700
    assert rows[0][0] % BAR_SECONDS == 0


def test_every_exchange_labels_a_bar_by_its_close():
    """Three payload shapes, one convention, or backtests silently disagree."""
    boundary = 1_757_675_700
    opened = boundary - BAR_SECONDS
    binance = data._parse_candles(
        "binance", [[opened * 1000, "1", "2", "0.5", "1.5", "10",
                     opened * 1000 + 299_999]], "BTCUSDT")
    coinbase = data._parse_candles(
        "coinbase", [[opened, 0.5, 2, 1, 1.5, 10]], "BTC-USD")
    kraken = data._parse_candles(
        "kraken", {"result": {"XXBTZUSD": [
            [opened, "1", "2", "0.5", "1.5", "0", "10"]]}}, "XBTUSD")
    assert binance[0][0] == coinbase[0][0] == kraken[0][0] == boundary


def test_gaps_finds_the_missing_stretches():
    ts = [300, 600, 900, 2_100, 2_400, 4_200]      # 4 missing, then 5 missing
    n = len(ts)
    series = data.BarSeries(ts=ts, open=[1.0] * n, high=[1.0] * n,
                            low=[1.0] * n, close=[1.0] * n, volume=[1.0] * n)
    assert series.gaps() == [(3, 3), (5, 5)]
    assert series.gap_count() == 2


def test_a_contiguous_series_has_no_gaps():
    series = data.synthetic(200, seed=3)
    assert series.gaps() == []
    assert series.gap_count() == 0
