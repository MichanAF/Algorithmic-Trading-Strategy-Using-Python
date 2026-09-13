"""Measure what each gate and each stack is actually worth.

This is the module that answers "which factors should we use?" with numbers
instead of opinion.  Point it at your own 5-minute history and it reports, for
every gate in the menu, how often its directional vote was right over the bet
horizon -- and for a set of candidate stacks, the accuracy and signal frequency
each one produces.

Read the ``z`` column before the accuracy column.  A gate that fires forty
times and hits 60% is indistinguishable from luck; one that fires eight
thousand times and hits 54% is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from . import indicators as ind
from .backtest import BARS_PER_DAY, resolve_outcome
from .config import StrategyConfig, config_from_dict
from .data import BarSeries
from .features import FeatureSet, build_features, minute_taker_share
from .gates import DOWN, FLAT, GATE_REGISTRY, UP
from .signal import SignalEngine


@dataclass
class EdgeStats:
    """Accuracy of a set of directional calls against what actually happened."""

    label: str
    signals: int
    wins: int
    losses: int
    voids: int
    break_even: float
    days: float

    @property
    def graded(self) -> int:
        return self.wins + self.losses

    @property
    def accuracy(self) -> float | None:
        return self.wins / self.graded if self.graded else None

    @property
    def stderr(self) -> float | None:
        acc = self.accuracy
        if acc is None or self.graded == 0:
            return None
        return math.sqrt(max(0.0, acc * (1.0 - acc)) / self.graded)

    @property
    def edge(self) -> float | None:
        acc = self.accuracy
        return None if acc is None else acc - self.break_even

    @property
    def z(self) -> float | None:
        """Standard errors between the hit rate and break-even."""
        edge, se = self.edge, self.stderr
        if edge is None or not se:
            return None
        return edge / se

    @property
    def per_day(self) -> float:
        return self.signals / max(1e-9, self.days)

    @property
    def verdict(self) -> str:
        z = self.z
        if z is None or self.graded < 100:
            return "too few signals to judge"
        if z >= 2.0:
            return "edge, significant"
        if z <= -2.0:
            return "negative edge, significant"
        return "no edge detectable"


def _horizon_outcomes(series: BarSeries, cfg: StrategyConfig) -> tuple[np.ndarray, int]:
    """Realised direction over the bet horizon, as UP/DOWN/0, per bar."""
    b = cfg.betting
    h = b.horizon_bars
    c = series.close
    out = np.zeros(len(c), dtype=int)
    if h < len(c):
        entry, exit_ = c[:-h], c[h:]
        with np.errstate(invalid="ignore", divide="ignore"):
            move_bps = np.where(entry > 0, (exit_ - entry) / entry * 10_000.0, 0.0)
        out[:-h] = np.where(np.abs(move_bps) < max(b.deadband_bps, 1e-12), 0,
                            np.where(move_bps > 0, 1, -1))
    return out, h


def _tally(label: str, calls: Sequence[tuple[int, int]], cfg: StrategyConfig,
           series: BarSeries, closes: np.ndarray) -> EdgeStats:
    """Grade (bar_index, side) calls using the backtest's own settlement rule."""
    b = cfg.betting
    wins = losses = voids = 0
    for i, side in calls:
        outcome = resolve_outcome(float(closes[i]), float(closes[i + b.horizon_bars]),
                                 side, b.deadband_bps, b.tie_policy)
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            voids += 1
    days = max(1e-9, len(series) / BARS_PER_DAY)
    return EdgeStats(label=label, signals=len(calls), wins=wins, losses=losses,
                     voids=voids, break_even=cfg.break_even_probability(), days=days)


def gate_edge(series: BarSeries, cfg: StrategyConfig,
              reference: BarSeries | None = None,
              features: FeatureSet | None = None,
              minute: BarSeries | None = None) -> list[EdgeStats]:
    """Per-gate directional accuracy over the bet horizon.

    Every gate in the registry is measured, not just the ones in the stack, so
    you can see what a candidate gate would have been worth before adding it.
    Confirmation gates are measured against the side they would confirm, using
    the stack's own proposed direction.
    """
    fs = (features if features is not None
          else build_features(series, cfg, reference, minute=minute))
    horizon = cfg.betting.horizon_bars
    closes = series.close
    last = len(series) - horizon
    engine = SignalEngine(cfg)
    warmup = max(engine.warmup_bars(fs), 1)

    stats: list[EdgeStats] = []
    for name, gate in sorted(GATE_REGISTRY.items()):
        if gate.is_veto:
            continue
        calls: list[tuple[int, int]] = []
        for i in range(warmup, last):
            if gate.is_confirm:
                proposed = engine.evaluate(fs, i).side
                if proposed == FLAT:
                    continue
                res = gate.evaluate(fs, i, cfg.gates, proposed)
            else:
                res = gate.evaluate(fs, i, cfg.gates)
            if res.passed and res.direction != FLAT:
                calls.append((i, res.direction))
        stats.append(_tally(name, calls, cfg, series, closes))
    return stats


def stack_edge(series: BarSeries, cfg: StrategyConfig,
               label: str | None = None,
               reference: BarSeries | None = None,
               features: FeatureSet | None = None) -> EdgeStats:
    """Accuracy of one full stack's tradable signals, before any risk sizing.

    Risk sizing is deliberately excluded: bankroll limits and cooldowns change
    how much you bet, not whether the signal was right.  Measure the signal
    first, then let the backtest tell you what the risk layer costs.
    """
    fs = features if features is not None else build_features(series, cfg, reference)
    engine = SignalEngine(cfg)
    horizon = cfg.betting.horizon_bars
    warmup = max(engine.warmup_bars(fs), 1)
    calls = []
    for i in range(warmup, len(series) - horizon):
        sig = engine.evaluate(fs, i)
        if sig.tradable:
            calls.append((i, sig.side))
    return _tally(label or cfg.name, calls, cfg, series, series.close)


def compare_stacks(series: BarSeries, stacks: dict[str, list[str]],
                   base: dict | None = None,
                   reference: BarSeries | None = None) -> list[EdgeStats]:
    """Score several candidate gate stacks on the same data.

    ``base`` is merged into each config, so you can compare stacks at a common
    payout and conviction floor.  The conviction floor and edge requirement are
    left as configured: they are part of the stack you are testing.
    """
    fs = build_features(series, config_from_dict(base, fit_stack=True), reference)
    out: list[EdgeStats] = []
    for label, names in stacks.items():
        overrides = _deep_copy(base or {})
        overrides["gate_stack"] = list(names)
        # fit_stack matters here: a three-gate stack carries one directional
        # gate, and the point of comparing is to include the small stacks.
        cfg = config_from_dict(overrides, fit_stack=True)
        out.append(stack_edge(series, cfg, label=label, features=fs))
    return out


def _deep_copy(node):
    if isinstance(node, dict):
        return {k: _deep_copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_deep_copy(v) for v in node]
    return node


def render_table(stats: Sequence[EdgeStats], title: str) -> str:
    """Fixed-width table, sorted by significance."""
    width = max(28, max((len(s.label) for s in stats), default=28) + 2)
    lines = [
        title,
        f"  {'':<{width}}{'signals':>9}{'per day':>9}{'accuracy':>10}"
        f"{'vs b/e':>9}{'z':>7}   verdict",
    ]
    for s in sorted(stats, key=lambda x: (x.z is None, -(x.z or 0.0))):
        acc = f"{s.accuracy:.2%}" if s.accuracy is not None else "n/a"
        edge = f"{s.edge:+.2%}" if s.edge is not None else "n/a"
        z = f"{s.z:+.1f}" if s.z is not None else "n/a"
        lines.append(f"  {s.label:<{width}}{s.signals:>9,}{s.per_day:>9.2f}"
                     f"{acc:>10}{edge:>9}{z:>7}   {s.verdict}")
    if stats:
        lines.append(f"  break-even to beat: {stats[0].break_even:.2%}"
                     f"   sample: {stats[0].days:.0f} days")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# order flow just before the window opens
# --------------------------------------------------------------------------- #
#
# The taker-flow gate reads one number: the share of volume that was aggressive
# buying.  Whether that number, read over the last minute or three before a
# window opens, says anything about the window's direction is a measurement,
# and this is where it is made -- on the development year only.  Nothing here
# chooses a parameter; it prints what each choice would have been worth so the
# person building the strategy can pick with evidence and write the pick down
# before the holdout is touched.


@dataclass
class FlowCorrelation:
    """How the pre-open taker share lines up with the window's direction."""

    minutes: int                        # 0 = the 5-minute bar's own share
    bars: int                           # graded windows; ties are excluded
    corr: float | None                  # Pearson, (share - 0.5) vs signed outcome
    share_before_up: float | None       # mean share ahead of windows that closed up
    share_before_down: float | None     # ...and ahead of those that closed down

    @property
    def label(self) -> str:
        return "full 5m bar" if self.minutes == 0 else f"last {self.minutes} min"

    @property
    def noise_band(self) -> float:
        """Two standard errors of a correlation at this sample size."""
        return 2.0 / math.sqrt(self.bars) if self.bars > 1 else float("inf")

    @property
    def reading(self) -> str:
        if self.corr is None or self.bars < 100:
            return "too few bars"
        if abs(self.corr) <= self.noise_band:
            return "noise"
        return "carries: follow" if self.corr > 0 else "reverts: fade"


@dataclass
class FlowCell:
    """One window length at one |z| floor, graded both ways."""

    minutes: int
    threshold: float
    follow: EdgeStats                   # a bet WITH the flow
    fade: EdgeStats                     # the same signals, bet AGAINST it

    @property
    def label(self) -> str:
        return "full 5m bar" if self.minutes == 0 else f"last {self.minutes} min"

    @property
    def better(self) -> EdgeStats:
        """Whichever side was right more often; they are exact complements."""
        f, d = self.follow.accuracy, self.fade.accuracy
        if f is None or d is None:
            return self.follow
        return self.fade if d > f else self.follow

    @property
    def better_mode(self) -> str:
        return "fade" if self.better is self.fade else "follow"


def _flow_share(series: BarSeries, minute: BarSeries | None,
                minutes: int) -> np.ndarray:
    """The share each window would have been scored on, per 5-minute bar."""
    n = len(series)
    if minutes == 0:
        if series.taker_buy is None:
            return np.full(n, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(series.volume > 0,
                            series.taker_buy / series.volume, np.nan)
    if minute is None:
        return np.full(n, np.nan)
    return minute_taker_share(series.ts, minute, minutes)


def flow_correlation(series: BarSeries, cfg: StrategyConfig,
                     minute: BarSeries | None = None,
                     windows: Sequence[int] = (1, 2, 3, 5, 0)
                     ) -> list[FlowCorrelation]:
    """Correlation of the pre-open taker share with the next window's direction.

    One number per window length, read before any threshold is applied: the
    sign says whether flow carries (positive: follow) or reverts (negative:
    fade), and the size against ``noise_band`` says whether it says anything.
    """
    outcome, horizon = _horizon_outcomes(series, cfg)
    last = max(0, len(series) - horizon)
    out: list[FlowCorrelation] = []
    for minutes in windows:
        share = _flow_share(series, minute, minutes)
        ok = np.isfinite(share) & (outcome != 0)
        ok[last:] = False
        n = int(ok.sum())
        corr = None
        if n >= 3 and np.std(share[ok]) > 0:
            corr = float(np.corrcoef(share[ok] - 0.5,
                                     outcome[ok].astype(float))[0, 1])
        up = share[ok & (outcome == UP)]
        down = share[ok & (outcome == DOWN)]
        out.append(FlowCorrelation(
            minutes=minutes, bars=n, corr=corr,
            share_before_up=float(up.mean()) if len(up) else None,
            share_before_down=float(down.mean()) if len(down) else None))
    return out


def flow_edge(series: BarSeries, cfg: StrategyConfig,
              minute: BarSeries | None = None,
              windows: Sequence[int] = (1, 2, 3, 5, 0),
              thresholds: Sequence[float] = (1.0, 1.5, 2.0, 2.5)
              ) -> list[FlowCell]:
    """Accuracy of betting with, and against, the pre-open flow at each |z| floor.

    The share is z-scored over ``gates.taker_flow.z_window`` bars exactly as
    the gate does it; a window fires when |z| clears the floor and the share is
    off 0.5.  No volume floor is applied here -- that is a separate decision,
    and folding it in would hide what the flow alone is worth.  Ties are void.
    """
    outcome, horizon = _horizon_outcomes(series, cfg)
    last = max(0, len(series) - horizon)
    days = max(1e-9, len(series) / BARS_PER_DAY)
    break_even = cfg.break_even_probability()
    z_window = cfg.gates.taker_flow.z_window
    cells: list[FlowCell] = []
    for minutes in windows:
        share = _flow_share(series, minute, minutes)
        z = (ind.zscore(share, z_window) if np.isfinite(share).any()
             else np.full(len(share), np.nan))
        side = np.where(share > 0.5, UP, np.where(share < 0.5, DOWN, FLAT))
        for threshold in thresholds:
            fire = np.isfinite(z) & (np.abs(z) >= threshold) & (side != FLAT)
            fire[last:] = False
            graded = fire & (outcome != 0)
            wins = int((graded & (side == outcome)).sum())
            losses = int((graded & (side == -outcome)).sum())
            voids = int((fire & (outcome == 0)).sum())
            signals = int(fire.sum())
            label = ("full 5m bar" if minutes == 0 else f"last {minutes} min"
                     ) + f" |z|>={threshold:g}"
            follow = EdgeStats(label=label + " follow", signals=signals,
                               wins=wins, losses=losses, voids=voids,
                               break_even=break_even, days=days)
            fade = EdgeStats(label=label + " fade", signals=signals,
                             wins=losses, losses=wins, voids=voids,
                             break_even=break_even, days=days)
            cells.append(FlowCell(minutes=minutes, threshold=threshold,
                                  follow=follow, fade=fade))
    return cells


def render_flow(corrs: Sequence[FlowCorrelation], cells: Sequence[FlowCell],
                title: str) -> str:
    """Two compact tables: the raw correlation, then the thresholded grid."""
    lines = [title, ""]
    lines.append(f"  {'window':<13}{'graded':>9}{'corr':>8}{'noise':>8}"
                 f"{'before UP':>11}{'before DOWN':>13}   reading")
    for c in corrs:
        corr = f"{c.corr:+.4f}" if c.corr is not None else "n/a"
        band = (f"±{c.noise_band:.4f}" if math.isfinite(c.noise_band)
                else "n/a")
        up = f"{c.share_before_up:.4f}" if c.share_before_up is not None else "n/a"
        down = (f"{c.share_before_down:.4f}" if c.share_before_down is not None
                else "n/a")
        lines.append(f"  {c.label:<13}{c.bars:>9,}{corr:>8}{band:>8}"
                     f"{up:>11}{down:>13}   {c.reading}")
    lines.append("")
    lines.append(f"  {'window':<13}{'|z|>=':>6}{'signals':>9}{'per day':>9}"
                 f"{'follow':>8}{'fade':>8}   better{'vs b/e':>9}{'z':>6}   verdict")
    for cell in cells:
        f, d, b = cell.follow, cell.fade, cell.better
        follow = f"{f.accuracy:.2%}" if f.accuracy is not None else "n/a"
        fade = f"{d.accuracy:.2%}" if d.accuracy is not None else "n/a"
        edge = f"{b.edge:+.2%}" if b.edge is not None else "n/a"
        z = f"{b.z:+.1f}" if b.z is not None else "n/a"
        lines.append(f"  {cell.label:<13}{cell.threshold:>6.1f}{f.signals:>9,}"
                     f"{f.per_day:>9.2f}{follow:>8}{fade:>8}   {cell.better_mode:<6}"
                     f"{edge:>9}{z:>6}   {b.verdict}")
    if cells:
        c0 = cells[0].follow
        lines.append(f"  break-even to beat: {c0.break_even:.2%}   sample: "
                     f"{c0.days:.0f} days   ties void, no volume floor")
    return "\n".join(lines)
