"""The 1-minute order-flow path: the minute share, the flow tables, the CLI.

Everything here serves one question -- does who was aggressing in the last
minute or three before a window opens say anything about the window? -- and
the machinery that answers it on the development year without touching a
holdout.  The planted fixtures put a known relationship into the last minute
so the tables can be checked for recovering it, and for reading its sign.
"""

import numpy as np
import pytest

from btc5m.attribution import flow_correlation, flow_edge, render_flow
from btc5m.cli import main
from btc5m.config import config_from_dict
from btc5m.data import BarSeries, synthetic
from btc5m.features import build_features, minute_taker_share

FLOW_CFG = {"gate_stack": ["data_integrity", "taker_flow", "session"],
            "betting": {"min_directional_gates": 1}}


def _minute_bars(ts, volume, taker) -> BarSeries:
    ones = np.ones(len(ts))
    return BarSeries(ts=ts, open=ones, high=ones, low=ones, close=ones,
                     volume=volume, taker_buy=taker)


def _minutes_for(five_ts, share_at, volume=10.0) -> BarSeries:
    """Five 1-minute bars per 5-minute close; ``share_at(i, k)`` is minute k's share."""
    ts, vol, taker = [], [], []
    for i, close_ts in enumerate(five_ts):
        for k in range(5):
            ts.append(int(close_ts) - 60 * (4 - k))
            vol.append(volume)
            taker.append(volume * share_at(i, k))
    return _minute_bars(ts, vol, taker)


def _with_taker(series: BarSeries, minute: BarSeries) -> BarSeries:
    """Give a 5m series the taker column its own archive would carry: the pooled sum."""
    share = minute_taker_share(series.ts, minute, 5)
    return BarSeries(ts=series.ts, open=series.open, high=series.high,
                     low=series.low, close=series.close, volume=series.volume,
                     taker_buy=share * series.volume)


def _planted(n: int = 3000, seed: int = 5, lean: float = 0.25):
    """The last minute before each open leans the way the bar will go.

    Every other minute is noise, so the signal dilutes as more of them are
    pooled -- which is the shape the real question has.
    """
    series = synthetic(n, seed=seed)
    rng = np.random.default_rng(seed)
    nxt = np.sign(np.diff(series.close, append=series.close[-1]))
    noise = rng.normal(0.0, 0.08, size=(n, 5))

    def share_at(i: int, k: int) -> float:
        s = 0.5 + noise[i, k] + (lean * nxt[i] if k == 4 else 0.0)
        return float(np.clip(s, 0.0, 1.0))

    minute = _minutes_for(series.ts, share_at)
    return _with_taker(series, minute), minute


# --------------------------------------------------------------------------- #
# the minute share itself
# --------------------------------------------------------------------------- #

def test_minute_share_pools_volume_rather_than_averaging_shares():
    """A busy minute weighs more than a quiet one."""
    minute = _minute_bars([540, 600], volume=[10.0, 30.0], taker=[10.0, 0.0])
    share = minute_taker_share(np.array([600]), minute, 2)
    assert share[0] == pytest.approx(10.0 / 40.0)      # not (1.0 + 0.0) / 2
    assert minute_taker_share(np.array([600]), minute, 1)[0] == 0.0


def test_minute_share_is_open_on_the_left_and_closed_on_the_right():
    """The minute closing exactly at the open counts; the one a window back does not."""
    minute = _minute_bars([420, 480, 540, 600, 660], volume=[10.0] * 5,
                          taker=[1.0, 2.0, 3.0, 4.0, 9.0])
    share = minute_taker_share(np.array([600]), minute, 3)
    assert share[0] == pytest.approx((2.0 + 3.0 + 4.0) / 30.0)   # 480, 540, 600
    # 660 closes after the open: it is the future and never read.
    assert minute_taker_share(np.array([600]), minute, 1)[0] == pytest.approx(0.4)


def test_a_missing_or_nan_minute_makes_the_window_nan():
    """A hole would silently shrink the window, so it voids it instead."""
    holed = _minute_bars([480, 600], volume=[10.0, 10.0], taker=[2.0, 4.0])
    assert np.isnan(minute_taker_share(np.array([600]), holed, 2)[0])
    assert minute_taker_share(np.array([600]), holed, 1)[0] == pytest.approx(0.4)

    blank = _minute_bars([480, 540, 600], volume=[10.0] * 3,
                         taker=[2.0, np.nan, 4.0])
    assert np.isnan(minute_taker_share(np.array([600]), blank, 2)[0])
    assert minute_taker_share(np.array([600]), blank, 1)[0] == pytest.approx(0.4)


def test_no_minute_series_or_no_taker_column_means_nan_everywhere():
    ts = np.array([600, 900])
    assert np.isnan(minute_taker_share(ts, None, 1)).all()
    ones = np.ones(2)
    no_taker = BarSeries(ts=[540, 600], open=ones, high=ones, low=ones,
                         close=ones, volume=ones)
    assert np.isnan(minute_taker_share(ts, no_taker, 1)).all()
    assert np.isnan(minute_taker_share(ts, no_taker, 0)).all()


def test_the_pooled_five_minutes_equal_the_bars_own_share():
    series, minute = _planted(400)
    own = series.taker_buy / series.volume
    five = minute_taker_share(series.ts, minute, 5)
    assert np.allclose(own, five)


# --------------------------------------------------------------------------- #
# the feature reads the minute series only when told to
# --------------------------------------------------------------------------- #

def test_features_read_the_minute_series_only_when_the_source_says_so():
    series, minute = _planted(600)
    own = series.taker_buy / series.volume
    cfg_bar = config_from_dict({**FLOW_CFG,
                                "gates": {"taker_flow": {"source": "5m"}}})
    cfg_min = config_from_dict({**FLOW_CFG,
                                "gates": {"taker_flow": {"source": "1m", "window": 1}}})

    from_bar = build_features(series, cfg_bar, minute=minute).values["taker_ratio"]
    assert np.allclose(from_bar, own)                  # the minute series is ignored

    from_min = build_features(series, cfg_min, minute=minute).values["taker_ratio"]
    assert np.allclose(from_min, minute_taker_share(series.ts, minute, 1))
    assert not np.allclose(from_min, own)

    without = build_features(series, cfg_min)
    assert np.isnan(without.values["taker_ratio"]).all()
    assert np.isnan(without.values["taker_z"]).all()
    # The gate never becomes ready, so it warms up forever rather than voting.
    assert without.warmup_bars(("taker_ratio", "taker_z")) == len(series)


def test_a_minute_sourced_backtest_runs_and_votes():
    """The gate's score saturates from min_abs_z upward, and a one-gate stack
    needs that score to clear the conviction floor, so the floor is set at
    zero here: the point is that the minute series reaches the bet, not what
    threshold to bet at."""
    from btc5m.backtest import run_backtest
    series, minute = _planted(2500)
    cfg = config_from_dict({**FLOW_CFG, "gates": {"taker_flow": {
        "source": "1m", "window": 1, "mode": "follow", "min_abs_z": 0.0,
        "min_volume_ratio": 0.0}}})
    with_minute = run_backtest(series, cfg, minute=minute)
    without = run_backtest(series, cfg)
    assert len(with_minute.bets) > 50
    assert with_minute.hit_rate > 0.9                # the planted lean is followed
    assert without.bets == []                        # no minutes, no vote


# --------------------------------------------------------------------------- #
# the two tables
# --------------------------------------------------------------------------- #

def test_flow_recovers_a_planted_last_minute_signal():
    series, minute = _planted(3000)
    cfg = config_from_dict(FLOW_CFG)
    corrs = {c.minutes: c for c in flow_correlation(series, cfg, minute)}
    assert corrs[1].corr > 0.5
    assert corrs[1].reading == "carries: follow"
    assert corrs[1].share_before_up > 0.6 > 0.4 > corrs[1].share_before_down
    # Pooling noise minutes dilutes it, monotonically.
    assert corrs[1].corr > corrs[2].corr > corrs[3].corr > corrs[5].corr > 0
    # The last five minutes are the bar, read from the other file.
    assert corrs[5].corr == pytest.approx(corrs[0].corr)
    assert corrs[1].noise_band == pytest.approx(2.0 / np.sqrt(corrs[1].bars))

    cells = flow_edge(series, cfg, minute, windows=(1,), thresholds=(1.0,))
    assert cells[0].better_mode == "follow"
    assert cells[0].follow.accuracy > 0.9
    assert cells[0].follow.signals > 500


def test_flow_reads_a_planted_reversal_as_fade():
    series, minute = _planted(3000, lean=-0.25)
    cfg = config_from_dict(FLOW_CFG)
    corr = flow_correlation(series, cfg, minute, windows=(1,))[0]
    assert corr.corr < -0.5
    assert corr.reading == "reverts: fade"
    cell = flow_edge(series, cfg, minute, windows=(1,), thresholds=(1.0,))[0]
    assert cell.better_mode == "fade"
    assert cell.fade.accuracy > 0.9


def test_follow_and_fade_are_exact_complements():
    series, minute = _planted(1500)
    cfg = config_from_dict(FLOW_CFG)
    for cell in flow_edge(series, cfg, minute):
        assert cell.fade.signals == cell.follow.signals
        assert cell.fade.wins == cell.follow.losses
        assert cell.fade.losses == cell.follow.wins
        assert cell.fade.voids == cell.follow.voids
        if cell.follow.graded:
            assert cell.fade.accuracy == pytest.approx(1.0 - cell.follow.accuracy)
        assert cell.better in (cell.follow, cell.fade)


def test_noise_reads_as_noise():
    """No relationship planted: the correlation sits inside its band."""
    series = synthetic(3000, seed=9)
    rng = np.random.default_rng(1)
    minute = _minutes_for(series.ts, lambda i, k: float(np.clip(
        0.5 + rng.normal(0.0, 0.1), 0.0, 1.0)))
    series = _with_taker(series, minute)
    cfg = config_from_dict(FLOW_CFG)
    corr = flow_correlation(series, cfg, minute, windows=(1,))[0]
    assert abs(corr.corr) < 3 * corr.noise_band       # generous: it is random
    assert corr.reading in ("noise", "carries: follow", "reverts: fade")


def test_flow_without_a_minute_series_scores_only_the_bar():
    series, _ = _planted(600)
    cfg = config_from_dict(FLOW_CFG)
    one, bar = flow_correlation(series, cfg, None, windows=(1, 0))
    assert one.bars == 0 and one.corr is None and one.reading == "too few bars"
    assert bar.bars > 500 and bar.corr is not None
    cells = flow_edge(series, cfg, None, windows=(1, 0), thresholds=(1.0,))
    assert cells[0].follow.signals == 0
    assert cells[1].follow.signals > 0


def test_render_flow_names_the_band_and_the_missing_volume_floor():
    series, minute = _planted(800)
    cfg = config_from_dict(FLOW_CFG)
    text = render_flow(flow_correlation(series, cfg, minute),
                       flow_edge(series, cfg, minute), "title line")
    assert text.startswith("title line")
    for needle in ("noise", "before UP", "last 1 min", "last 3 min",
                   "full 5m bar", "|z|>=", "follow", "fade", "no volume floor",
                   "break-even to beat"):
        assert needle in text, needle


# --------------------------------------------------------------------------- #
# config and command line
# --------------------------------------------------------------------------- #

def test_config_refuses_bad_taker_flow_values():
    for bad in ({"mode": "invert"}, {"source": "15m"}, {"window": 0},
                {"z_window": 1}):
        with pytest.raises(ValueError, match="taker_flow"):
            config_from_dict({"gates": {"taker_flow": bad}})
    ok = config_from_dict({"gates": {"taker_flow": {"source": "1m", "window": 3,
                                                    "mode": "fade"}}})
    assert (ok.gates.taker_flow.source, ok.gates.taker_flow.window) == ("1m", 3)


def test_cli_flow_runs_on_csv_files(tmp_path, capsys):
    series, minute = _planted(1200)
    five, one = tmp_path / "five.csv", tmp_path / "one.csv"
    series.write_csv(five)
    minute.write_csv(one)
    code = main(["flow", "--data", str(five), "--minute", str(one),
                 "--windows", "1,5,0", "--thresholds", "1,2"])
    out = capsys.readouterr()
    assert code == 0
    assert "last 1 min" in out.out and "full 5m bar" in out.out
    assert "carries: follow" in out.out
    assert "development-year measurement" in out.out


def test_cli_flow_refuses_a_five_minute_file_as_minutes(tmp_path):
    series, _ = _planted(500)
    five = tmp_path / "five.csv"
    series.write_csv(five)
    with pytest.raises(SystemExit, match="not 1-minute"):
        main(["flow", "--data", str(five), "--minute", str(five)])


def test_cli_flow_refuses_a_minute_file_without_taker_volume(tmp_path):
    series, minute = _planted(500)
    five, one = tmp_path / "five.csv", tmp_path / "one.csv"
    series.write_csv(five)
    ones = np.ones(len(minute))
    BarSeries(ts=minute.ts, open=ones, high=ones, low=ones, close=ones,
              volume=minute.volume).write_csv(one)
    with pytest.raises(SystemExit, match="no taker_buy column"):
        main(["flow", "--data", str(five), "--minute", str(one)])


def test_cli_flow_skips_minute_windows_without_a_minute_file(tmp_path, capsys):
    series, _ = _planted(600)
    five = tmp_path / "five.csv"
    series.write_csv(five)
    code = main(["flow", "--data", str(five), "--windows", "1,0"])
    out = capsys.readouterr()
    assert code == 0
    assert "skipped" in out.err
    assert "full 5m bar" in out.out and "last 1 min" not in out.out
    with pytest.raises(SystemExit, match="nothing to measure"):
        main(["flow", "--data", str(five), "--windows", "1,2"])


def test_cli_flow_validates_its_lists(tmp_path):
    series, _ = _planted(500)
    five = tmp_path / "five.csv"
    series.write_csv(five)
    with pytest.raises(SystemExit, match="comma-separated numbers"):
        main(["flow", "--data", str(five), "--windows", "one"])
    with pytest.raises(SystemExit, match="minutes >= 1"):
        main(["flow", "--data", str(five), "--windows", "-1"])


def test_cli_notes_a_minute_source_with_no_minute_file(capsys):
    code = main(["backtest", "--synthetic", "1500",
                 "--gates", "data_integrity,taker_flow,session",
                 "--set", "gates.taker_flow.source=1m"])
    assert code == 0
    assert "never votes" in capsys.readouterr().err
    code = main(["gates", "--synthetic", "1500",
                 "--gates", "data_integrity,taker_flow,session",
                 "--set", "gates.taker_flow.source=1m"])
    assert code == 0
    assert "never votes" in capsys.readouterr().err
