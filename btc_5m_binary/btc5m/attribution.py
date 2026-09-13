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

from .backtest import BARS_PER_DAY, resolve_outcome
from .config import StrategyConfig, config_from_dict
from .data import BarSeries
from .features import FeatureSet, build_features
from .gates import FLAT, GATE_REGISTRY
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
              features: FeatureSet | None = None) -> list[EdgeStats]:
    """Per-gate directional accuracy over the bet horizon.

    Every gate in the registry is measured, not just the ones in the stack, so
    you can see what a candidate gate would have been worth before adding it.
    Confirmation gates are measured against the side they would confirm, using
    the stack's own proposed direction.
    """
    fs = features if features is not None else build_features(series, cfg, reference)
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
