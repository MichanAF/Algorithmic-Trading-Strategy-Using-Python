"""The forward slice decides which bars a result is computed on, so it is tested.

The claim that matters is the one the printed lines make: the first bar the
engine grades is the first bar after the cutoff, with the warm-up entirely on
the already-seen side of it.  Everything else here is the refusals.
"""

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


def test_a_plain_cut_keeps_only_bars_after_the_cutoff(tmp_path):
    """No config, no warm-up: what the minute series for `settlement` wants."""
    series = bars_with_flow(600, CUTOFF_TS + 300 * 200)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path),
                              "--cutoff", CUTOFF, "-o", out]) == 0
    from btc5m.data import load_csv
    cut = load_csv(out)
    assert len(cut) == 200
    assert int(cut.ts[0]) == CUTOFF_TS + 300
    assert cut.taker_buy is not None      # the flow gate's only input survives


def test_a_bar_closing_exactly_at_the_cutoff_counts_as_seen(tmp_path):
    """`fetch --end` keeps the bar closing at the cutoff, so the slice must not."""
    series = bars_with_flow(400, CUTOFF_TS)
    out = str(tmp_path / "new.csv")
    assert slice_forward.main(["--data", write(series, tmp_path),
                              "--cutoff", CUTOFF, "-o", out]) == 2


def test_the_first_graded_bar_is_the_first_bar_after_the_cutoff(tmp_path):
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
    assert int(cut.ts[warmup]) == CUTOFF_TS + 300
    assert int(cut.ts[warmup - 1]) <= CUTOFF_TS


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
    assert "nothing after" in capsys.readouterr().err


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
