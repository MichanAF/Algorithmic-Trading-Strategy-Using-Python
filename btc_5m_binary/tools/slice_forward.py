#!/usr/bin/env python3
"""Cut the forward slice: the bars this repository has never backtested.

Every backtest here runs on a year cut with ``fetch --end``, and the newest of
those years ends at 2026-09-13 00:00 UTC.  Bars after that instant have been
read by nothing in this repository, so they are the one out-of-sample test
available that costs none of the pre-registered years still untouched.

A slice cannot simply start at the cutoff.  The gates need a warm-up -- a day
of taker-flow z-scores, 288 bars -- so a slice starting at the cutoff spends
its first day scoring nothing, while a slice starting a day early places bets
inside the year already seen.  So this asks the engine how many bars it needs
and keeps exactly that many from before the cutoff: the first bar the backtest
grades is then the first bar nobody has read, which the printed lines state
rather than leave to be assumed.

Given no --config it does the plain thing instead and keeps only the bars after
the cutoff, which is what the minute series for ``settlement`` needs: that path
does arithmetic on whole windows and has no warm-up.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from btc5m.config import StrategyConfig, load_config
from btc5m.data import BarSeries, load_csv


def parse_cutoff(text: str) -> int:
    """A YYYY-MM-DD cutoff, read as midnight UTC, epoch seconds.

    The same instant ``fetch --end`` uses, so a slice cut here and a year cut
    with the same date are contiguous and never overlap: that year ends with
    the last bar closing at or before the cutoff, and this slice holds the
    bars closing after it.
    """
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"error: --cutoff must be YYYY-MM-DD, got {text!r}") from exc
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def warmup_bars(series: BarSeries, cfg: StrategyConfig) -> int:
    """How many leading bars the config's gates consume before they can score."""
    from btc5m.features import build_features
    from btc5m.signal import SignalEngine

    fs = build_features(series, cfg)
    return max(SignalEngine(cfg).warmup_bars(fs), 1)


def first_after(ts: np.ndarray, cutoff: int) -> int:
    """Index of the first bar closing strictly after ``cutoff``."""
    later = np.nonzero(ts > cutoff)[0]
    return int(later[0]) if len(later) else len(ts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, metavar="CSV",
                    help="bars to cut, as written by `btc5m fetch`")
    ap.add_argument("--cutoff", required=True, metavar="YYYY-MM-DD",
                    help="the last --end already backtested; bars after it are the slice")
    ap.add_argument("--config", metavar="JSON",
                    help="keep this config's warm-up before the cutoff "
                         "(omit for a plain cut, which is what minute bars want)")
    ap.add_argument("-o", "--out", required=True, metavar="CSV")
    args = ap.parse_args(argv)

    # The loader owns the invariants -- ascending timestamps, no ragged columns --
    # and a slice of a file that breaks one would not be the span it claims, so
    # the refusal is reported rather than raised as a traceback in a CI log.
    try:
        series = load_csv(args.data)
    except ValueError as exc:
        print(f"error: {args.data} cannot be sliced: {exc}", file=sys.stderr)
        return 2

    cutoff = parse_cutoff(args.cutoff)
    new = first_after(series.ts, cutoff)
    if new >= len(series):
        print(f"error: {args.data} holds nothing after {args.cutoff} "
              f"(it ends {series.time_at(-1):%Y-%m-%d %H:%M} UTC), so there is "
              f"no forward slice to cut yet", file=sys.stderr)
        return 2

    warmup = warmup_bars(series, load_config(args.config)) if args.config else 0
    start = new - warmup
    if start < 0:
        print(f"error: the gates need {warmup:,} bars of warm-up before "
              f"{args.cutoff} and only {new:,} are in {args.data}. Fetch "
              f"{warmup - new:,} more bars of history.", file=sys.stderr)
        return 2

    cut = series[start:]
    path = cut.write_csv(args.out)
    graded = warmup                       # the first bar the engine can score
    print(f"forward slice  {args.data} -> {path}")
    print(f"  cutoff       {args.cutoff} 00:00 UTC, already backtested up to here")
    print(f"  warm-up      {warmup:,} bars kept before the cutoff"
          f"{' (none asked for)' if not warmup else ''}")
    print(f"  wrote        {len(cut):,} bars, "
          f"{cut.time_at(0):%Y-%m-%d %H:%M} .. {cut.time_at(-1):%Y-%m-%d %H:%M} UTC")
    print(f"  first graded {cut.time_at(graded):%Y-%m-%d %H:%M} UTC, "
          f"{len(cut) - graded:,} bars "
          f"({(len(cut) - graded) * cut.bar_seconds / 86_400:.2f} days) never read")
    gaps = cut.gaps()
    if gaps:
        print(f"  {len(gaps)} gap(s), {sum(n for _, n in gaps):,} bars missing; "
              f"data_integrity vetoes the bar after each one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
