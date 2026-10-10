"""The forward slice decides which bars a result is computed on, so it is tested.

The claim that matters is the one the printed lines make: the first bar the
engine grades is the first bar after the cutoff, with the warm-up entirely on
the already-seen side of it.  Everything else here is the refusals.
"""

import pathlib

import numpy as np
import pytest

from btc5m.config import load_config
from btc5m.data import synthetic
from btc5m.features import build_features
from btc5m.signal import SignalEngine
from conftest import config_path
from tools import slice_forward

CUTOFF = "2026-09-13"
CUTOFF_TS = 1_789_257_600      # midnight UTC, checked against parse_cutoff below
CONFIG = config_path("fade-flow-pooled-5m.json")


def bars_with_flow(n: int, end_ts: int, seed: int = 11):
    """Synthetic bars carrying a taker-buy column, which synthetic() has none of.

    The taker share has to vary: the flow gate z-scores it over a day, and a
    constant share has zero variance, so its z would never be finite and the
    warm-up would swallow the whole series.
    """
    series = synthetic(n, seed=seed)
    rng = np.random.default_rng(seed)
    share = 0.5 + 0.08 * rng.standard_normal(n)
    series.taker_buy = np.clip(share, 0.05, 0.95) * series.volume
    series.ts = np.arange(end_ts - (n - 1) * 300, end_ts + 1, 300, dtype=np.int64)
    return series


def write(series, tmp_path, name="tail.csv"):
    return str(series.write_csv(tmp_path / name))


def test_the_cutoff_is_the_same_instant_fetch_end_uses():
    assert slice_forward.parse_cutoff(CUTOFF) == CUTOFF_TS
    with pytest.raises(SystemExit):
        slice_forward.parse_cutoff("13-09-2026")


def test_a_plain_cut_starts_at_the_cutoff_bar(tmp_path):
    """No config, no warm-up: what the minute series for `settlement` wants."""
    series = bars_with_flow(600, CUTOFF_TS + 300 * 200)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path),
                              "--cutoff", CUTOFF, "-o", out]) == 0
    from btc5m.data import load_csv
    cut = load_csv(out)
    assert len(cut) == 201                # the cutoff bar plus the 200 after it
    assert int(cut.ts[0]) == CUTOFF_TS
    assert cut.taker_buy is not None      # the flow gate's only input survives


def test_a_tail_that_stops_at_the_cutoff_has_nothing_to_grade(tmp_path, capsys):
    """The cutoff bar is the first ungraded one, but settling it needs the next."""
    series = bars_with_flow(400, CUTOFF_TS)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path),
                              "--cutoff", CUTOFF, "-o", out]) == 2
    assert "nothing to grade" in capsys.readouterr().err
    assert not pathlib.Path(out).exists()


def test_the_slice_grades_the_one_bar_a_year_cut_here_could_not(tmp_path):
    """No bar falls between what the year graded and what the slice grades.

    A year cut with `fetch --end CUTOFF` ends on the bar closing at the cutoff
    and grades up to the one before it.  So the slice must grade that last bar
    of the year's series, not the one after: otherwise exactly one bar per
    boundary is scored by nothing, and the weekly increments stop summing to
    the span.
    """
    from btc5m.data import load_csv

    series = bars_with_flow(2600, CUTOFF_TS + 300 * 1700)
    year = series[: int(np.nonzero(series.ts <= CUTOFF_TS)[0][-1]) + 1]
    assert int(year.ts[-1]) == CUTOFF_TS, "the year cut holds the cutoff bar"

    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path), "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", out]) == 0
    cut = load_csv(out)
    cfg = load_config(CONFIG)
    warmup = max(SignalEngine(cfg).warmup_bars(build_features(cut, cfg)), 1)

    # the year's last gradeable anchor, and the slice's first, are adjacent
    assert int(year.ts[-2]) + 300 == int(cut.ts[warmup])
    assert int(cut.ts[warmup]) == int(year.ts[-1]) == CUTOFF_TS


def test_the_first_graded_bar_is_the_first_bar_nobody_graded(tmp_path):
    """The whole point: warm-up on the seen side, every graded bar on the new side."""
    series = bars_with_flow(2600, CUTOFF_TS + 300 * 1700)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path), "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", out]) == 0

    from btc5m.data import load_csv
    cut = load_csv(out)
    cfg = load_config(CONFIG)
    warmup = max(SignalEngine(cfg).warmup_bars(build_features(cut, cfg)), 1)
    assert warmup > 1, "a config whose gates need no history would make this vacuous"
    assert int(cut.ts[warmup]) == CUTOFF_TS
    assert int(cut.ts[warmup - 1]) == CUTOFF_TS - 300


def test_too_little_history_before_the_cutoff_is_refused(tmp_path, capsys):
    """Silently starting the warm-up after the cutoff would waste the new bars."""
    series = bars_with_flow(400, CUTOFF_TS + 300 * 390)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path), "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", out]) == 2
    err = capsys.readouterr().err
    assert "warm-up" in err and "more bars" in err


def test_nothing_after_the_cutoff_is_refused(tmp_path, capsys):
    series = bars_with_flow(400, CUTOFF_TS - 300)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path),
                              "--cutoff", CUTOFF, "-o", out]) == 2
    assert "nothing to grade" in capsys.readouterr().err


def test_a_csv_the_loader_refuses_is_reported_not_raised(tmp_path, capsys):
    """Ascending timestamps are the loader's invariant; a CI log wants the reason."""
    bad = tmp_path / "unordered.csv"
    bad.write_text("timestamp,open,high,low,close,volume\n"
                   "1789257900,1,1,1,1,1\n"
                   "1789257600,1,1,1,1,1\n")
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", str(bad),
                              "--cutoff", CUTOFF, "-o", out]) == 2
    err = capsys.readouterr().err
    assert "cannot be sliced" in err and "increasing" in err


def test_a_slice_straddling_a_hole_is_refused(tmp_path, capsys):
    """The failure that got through: real bars either side, a month missing between.

    A hole is worse than a short series.  data_integrity vetoes the one bar
    after a gap and nothing else notices, so a backtest over 287 bars from
    before the cutoff and 288 from three weeks later reported a hit rate and
    called itself two days.  The slice has to refuse instead.
    """
    before = bars_with_flow(400, CUTOFF_TS, seed=3)
    after = bars_with_flow(288, CUTOFF_TS + 86_400 * 18, seed=4)
    joined = tmp_path / "holed.csv"
    rows = [f"{int(s.ts[i])},{s.open[i]:.2f},{s.high[i]:.2f},{s.low[i]:.2f},"
            f"{s.close[i]:.2f},{s.volume[i]:.6f},{s.taker_buy[i]:.6f}"
            for s in (before, after) for i in range(len(s))]
    joined.write_text("timestamp,open,high,low,close,volume,taker_buy\n"
                      + "\n".join(rows) + "\n")
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", str(joined), "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", out]) == 2
    err = capsys.readouterr().err
    assert "missing" in err and "hole" in err
    assert not pathlib.Path(out).exists(), "a refused slice must write no CSV"


def test_a_contiguous_slice_reports_full_coverage(tmp_path, capsys):
    series = bars_with_flow(2600, CUTOFF_TS + 300 * 1700)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path), "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", out]) == 0
    assert "coverage     100.00%" in capsys.readouterr().out


def test_from_grades_a_later_window_of_the_same_span(tmp_path, capsys):
    """--from measures the days a previous reading missed instead of deriving them."""
    from btc5m.data import load_csv

    series = bars_with_flow(5000, CUTOFF_TS + 300 * 4000)
    data = write(series, tmp_path)
    later = "2026-09-20"                      # a week past the cutoff
    out = str(tmp_path / "newest.csv")
    assert slice_forward.main(["--data", data, "--cutoff", CUTOFF,
                              "--from", later, "--config", CONFIG, "-o", out]) == 0

    cut = load_csv(out)
    cfg = load_config(CONFIG)
    warmup = max(SignalEngine(cfg).warmup_bars(build_features(cut, cfg)), 1)
    assert int(cut.ts[warmup]) == slice_forward.parse_cutoff(later)
    assert int(cut.ts[warmup - 1]) == slice_forward.parse_cutoff(later) - 300
    printed = capsys.readouterr().out
    assert f"graded from  {later}" in printed


def test_from_before_the_cutoff_is_refused(tmp_path, capsys):
    """Moving the start earlier would score a year the protocol has already spent."""
    series = bars_with_flow(5000, CUTOFF_TS + 300 * 4000)
    out = str(tmp_path / "newest.csv")
    assert slice_forward.main(["--data", write(series, tmp_path), "--cutoff", CUTOFF,
                              "--from", "2026-09-01", "--config", CONFIG,
                              "-o", out]) == 2
    err = capsys.readouterr().err
    assert "before --cutoff" in err and "already read" in err
    assert not pathlib.Path(out).exists()


def test_from_equal_to_the_cutoff_is_the_ordinary_slice(tmp_path):
    from btc5m.data import load_csv

    series = bars_with_flow(2600, CUTOFF_TS + 300 * 1700)
    data = write(series, tmp_path)
    plain, dated = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    assert slice_forward.main(["--data", data, "--cutoff", CUTOFF,
                              "--config", CONFIG, "-o", plain]) == 0
    assert slice_forward.main(["--data", data, "--cutoff", CUTOFF, "--from", CUTOFF,
                              "--config", CONFIG, "-o", dated]) == 0
    assert pathlib.Path(plain).read_text() == pathlib.Path(dated).read_text()


def test_a_slice_scores_the_same_signals_as_a_longer_series(tmp_path):
    """Where the slice starts must not change what the gates say on its bars.

    The whole forward-slice method rests on this: a 20-day slice and a 27-day
    slice must agree, bar for bar, on the days they share, or each reading
    would silently rescore the last one.  Many features do drift with how much
    history precedes them -- EMAs, OBV and the higher-timeframe series are
    seeded rather than windowed -- but this config's gates read none of them,
    and the test fails if that ever stops being true.
    """
    full = bars_with_flow(6000, CUTOFF_TS + 300 * 5000, seed=17)
    cfg = load_config(CONFIG)
    fs_full = build_features(full, cfg)
    engine = SignalEngine(cfg)
    warmup = max(engine.warmup_bars(fs_full), 1)

    for tail in (2303, 5759):
        cut = full[len(full) - tail:]
        fs_cut = build_features(cut, cfg)
        offset = len(full) - tail
        assert max(engine.warmup_bars(fs_cut), 1) == warmup
        long_side = [engine.evaluate(fs_full, i).tradable
                     for i in range(offset + warmup, len(full) - 1)]
        slice_side = [engine.evaluate(fs_cut, i).tradable
                      for i in range(warmup, tail - 1)]
        assert long_side == slice_side, (
            f"tail {tail}: {sum(long_side)} signals in the long series, "
            f"{sum(slice_side)} in the slice")
