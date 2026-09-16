"""Are the gates and conditions actually independent, or confirming one thing?

A stack of five gates is only as selective as the number of *independent*
questions it asks.  Two gates that fire on the same bars for the same underlying
reason do not double the evidence; they halve the signal count while adding
nothing, and they make the stack look more confirmed than it is.

Four measurements, weakest to strongest:

``structural_overlap``  which gates read the same features.  Cheap, and a hard
                        floor: gates sharing an input cannot be independent.
``verdict_overlap``     do they pass on the same bars, and vote the same way?
                        Gates can share no features and still be near-duplicates.
``marginal_value``      the one that decides.  Among bars where every *other*
                        gate already passed, does this gate's verdict still
                        separate winners from losers?  If not, it is redundant
                        no matter how independent it looks.
``leave_one_out``       what the whole stack does without each gate.

The betting and risk conditions get the same treatment via
``condition_bindings``, which catches a condition that can never be the binding
one -- a check that looks like a safeguard but is dead code in practice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .attribution import EdgeStats, _horizon_outcomes
from .backtest import BARS_PER_DAY
from .config import StrategyConfig, config_from_dict
from .data import BarSeries
from .features import FeatureSet, build_features
from .gates import FLAT, GATE_REGISTRY, build_stack
from .signal import SignalEngine


# --------------------------------------------------------------------------- #
# one pass over the bars, then everything else is vectorised
# --------------------------------------------------------------------------- #


@dataclass
class Verdicts:
    """Per-gate pass/direction arrays plus the realised outcome, aligned by bar."""

    names: list[str]
    passed: dict[str, np.ndarray]
    direction: dict[str, np.ndarray]
    proposed: np.ndarray
    outcome: np.ndarray
    start: int
    bars: int

    break_even: float = 0.5

    @property
    def days(self) -> float:
        return max(1e-9, self.bars / BARS_PER_DAY)

    def graded(self) -> np.ndarray:
        """Bars whose outcome is a decided up or down move."""
        return self.outcome != 0


def collect_verdicts(series: BarSeries, cfg: StrategyConfig,
                     gate_names: Sequence[str] | None = None,
                     reference: BarSeries | None = None,
                     features: FeatureSet | None = None) -> Verdicts:
    """Evaluate every gate on every bar once, into arrays.

    Confirmation gates are evaluated against the side the configured stack's
    directional gates proposed, since that is the only side they could ever be
    asked about.
    """
    fs = features if features is not None else build_features(series, cfg, reference)
    names = list(gate_names) if gate_names else sorted(GATE_REGISTRY)
    gates = [GATE_REGISTRY[n] for n in names]

    engine = SignalEngine(cfg)
    outcome, horizon = _horizon_outcomes(series, cfg)
    start = max(engine.warmup_bars(fs), 1)
    # Every gate in `names` must be warm too, not just the configured stack.
    extra = tuple(dict.fromkeys(r for g in gates for r in g.requires
                                if np.isfinite(fs.values[r]).any()))
    if extra:
        start = max(start, fs.warmup_bars(extra))
    end = len(series) - horizon
    n = max(0, end - start)

    passed = {name: np.zeros(n, dtype=bool) for name in names}
    direction = {name: np.zeros(n, dtype=np.int8) for name in names}
    proposed = np.zeros(n, dtype=np.int8)

    first_pass = [g for g in gates if not g.is_confirm]
    confirmers = [g for g in gates if g.is_confirm]

    for k, i in enumerate(range(start, end)):
        for gate in first_pass:
            res = gate.evaluate(fs, i, cfg.gates)
            passed[gate.name][k] = res.passed
            direction[gate.name][k] = res.direction

        # The side the directional gates point to, on their own: no conviction
        # floor, no payout test, no confirmation gates.  Analysis wants raw gate
        # behaviour, and a confirmation gate judged against a side the full
        # engine already approved would be reasoning in a circle.
        sides = {direction[g.name][k] for g in first_pass if g.is_directional
                 and passed[g.name][k]}
        all_passed = all(passed[g.name][k] for g in first_pass if g.is_directional)
        side = sides.pop() if all_passed and len(sides) == 1 else FLAT
        proposed[k] = side

        for gate in confirmers:
            res = gate.evaluate(fs, i, cfg.gates, side)
            passed[gate.name][k] = res.passed
            direction[gate.name][k] = res.direction

    return Verdicts(names=names, passed=passed, direction=direction,
                    proposed=proposed, outcome=outcome[start:end],
                    start=start, bars=n, break_even=cfg.break_even_probability())


# --------------------------------------------------------------------------- #
# 1. structural overlap
# --------------------------------------------------------------------------- #


@dataclass
class SharedInputs:
    a: str
    b: str
    shared: tuple[str, ...]
    share_of_a: float
    share_of_b: float


def structural_overlap(gate_names: Sequence[str]) -> list[SharedInputs]:
    """Gate pairs that read the same features.  A hard floor on independence."""
    out: list[SharedInputs] = []
    names = list(gate_names)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ra = set(GATE_REGISTRY[a].requires)
            rb = set(GATE_REGISTRY[b].requires)
            shared = ra & rb
            if shared:
                out.append(SharedInputs(a, b, tuple(sorted(shared)),
                                        len(shared) / len(ra),
                                        len(shared) / len(rb)))
    return sorted(out, key=lambda s: -len(s.shared))


# --------------------------------------------------------------------------- #
# 2. verdict overlap
# --------------------------------------------------------------------------- #


@dataclass
class PairOverlap:
    a: str
    b: str
    phi: float                  # correlation of the two pass/fail series
    jaccard: float              # |both pass| / |either passes|
    p_b: float                  # P(B passes)
    p_b_given_a: float          # P(B passes | A passes)
    direction_agreement: float | None   # when both pass and both have a side
    both_pass: int

    @property
    def lift(self) -> float:
        return self.p_b_given_a / self.p_b if self.p_b > 0 else float("nan")

    @property
    def verdict(self) -> str:
        if self.both_pass < 100:
            return "too few joint passes to judge"
        if self.phi >= 0.7 or (self.direction_agreement or 0) >= 0.97:
            return "near-duplicate"
        if self.phi >= 0.4 or (self.direction_agreement or 0) >= 0.9:
            return "substantial overlap"
        if self.phi <= -0.4:
            return "opposed (rarely co-fire)"
        return "largely independent"


def _phi(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of two boolean series; 0 when either is constant."""
    if a.size < 2:
        return float("nan")
    fa, fb = a.astype(float), b.astype(float)
    sa, sb = fa.std(), fb.std()
    if sa == 0 or sb == 0:
        return 0.0
    return float(np.corrcoef(fa, fb)[0, 1])


def verdict_overlap(v: Verdicts) -> list[PairOverlap]:
    """How much do gate verdicts coincide, and do they vote the same way?"""
    out: list[PairOverlap] = []
    for i, a in enumerate(v.names):
        for b in v.names[i + 1:]:
            pa, pb = v.passed[a], v.passed[b]
            both = pa & pb
            either = pa | pb
            da, db = v.direction[a], v.direction[b]
            sided = both & (da != FLAT) & (db != FLAT)
            agreement = (float(np.mean(da[sided] == db[sided]))
                         if sided.sum() >= 30 else None)
            out.append(PairOverlap(
                a=a, b=b,
                phi=_phi(pa, pb),
                jaccard=float(both.sum() / either.sum()) if either.any() else 0.0,
                p_b=float(pb.mean()),
                p_b_given_a=float(pb[pa].mean()) if pa.any() else float("nan"),
                direction_agreement=agreement,
                both_pass=int(both.sum()),
            ))
    return sorted(out, key=lambda p: -abs(p.phi))


# --------------------------------------------------------------------------- #
# 3. marginal value -- the measurement that decides
# --------------------------------------------------------------------------- #


@dataclass
class MarginalValue:
    gate: str
    rest_passes: int
    with_gate: int
    without_gate: int
    hit_with: float | None
    hit_without: float | None

    @property
    def separation(self) -> float | None:
        """Hit rate when this gate agrees, minus when it does not."""
        if self.hit_with is None or self.hit_without is None:
            return None
        return self.hit_with - self.hit_without

    @property
    def stderr(self) -> float | None:
        if self.hit_with is None or self.hit_without is None:
            return None
        if not self.with_gate or not self.without_gate:
            return None
        va = self.hit_with * (1 - self.hit_with) / self.with_gate
        vb = self.hit_without * (1 - self.hit_without) / self.without_gate
        se = math.sqrt(va + vb)
        return se if se > 0 else None

    @property
    def z(self) -> float | None:
        sep, se = self.separation, self.stderr
        return None if sep is None or not se else sep / se

    @property
    def signals_filtered(self) -> float | None:
        """Share of otherwise-passing bars this gate removes."""
        if not self.rest_passes:
            return None
        return self.without_gate / self.rest_passes

    @property
    def verdict(self) -> str:
        if min(self.with_gate, self.without_gate) < 50:
            return "too few bars to judge"
        z = self.z
        if z is None:
            return "no measurement"
        if z >= 2.0:
            return "adds information"
        if z <= -2.0:
            return "inverted: the failing side wins more"
        return "no information beyond the other gates"


def marginal_value(v: Verdicts, stack: Sequence[str] | None = None
                   ) -> list[MarginalValue]:
    """For each gate: given the others already agreed, does its verdict matter?

    This is the honest test for redundancy.  The side is taken from the *other*
    gates, so both buckets exist: bars where this gate confirmed that side, and
    bars where it did not.  If the hit rate is the same either way, the gate adds
    nothing the rest of the stack had not already captured -- however unique its
    inputs look.

    Only directional and confirmation gates are scored; a veto gate has no side
    to be right about.
    """
    names = list(stack) if stack else v.names
    graded = v.graded()
    out: list[MarginalValue] = []

    for name in names:
        if GATE_REGISTRY[name].is_veto:
            continue
        others = [n for n in names if n != name]
        directional_others = [n for n in others
                              if GATE_REGISTRY[n].is_directional]
        if not directional_others:
            continue

        # Bars where every other gate passed and the other directional gates
        # agree on one side.
        rest = graded.copy()
        for other in others:
            rest &= v.passed[other]
        side_rest = np.zeros(v.bars, dtype=np.int8)
        first = v.direction[directional_others[0]]
        agree = np.ones(v.bars, dtype=bool)
        for other in directional_others[1:]:
            agree &= v.direction[other] == first
        side_rest = np.where(agree, first, 0).astype(np.int8)
        rest &= agree & (side_rest != FLAT)

        confirms = rest & v.passed[name] & (v.direction[name] == side_rest)
        declines = rest & ~confirms
        correct = side_rest == v.outcome

        def rate(mask: np.ndarray) -> float | None:
            return float(np.mean(correct[mask])) if mask.sum() else None

        out.append(MarginalValue(
            gate=name, rest_passes=int(rest.sum()),
            with_gate=int(confirms.sum()), without_gate=int(declines.sum()),
            hit_with=rate(confirms), hit_without=rate(declines)))
    return sorted(out, key=lambda m: (m.z is None, -(m.z or 0.0)))


# --------------------------------------------------------------------------- #
# 3b. how two directional gates combine
# --------------------------------------------------------------------------- #
#
# A stack with two directional gates can be wired two ways.  In unanimous mode
# every directional gate must pass, so a bar is only tradable when both fire
# and agree: AND.  In weighted mode a gate that does not fire simply casts no
# vote, so either gate alone can carry a bar, and a disagreement is refused
# unless dissent is allowed: OR.  Which wiring to choose is a decision, and the
# cells below are what it is decided on -- each gate alone, both agreeing,
# both disagreeing, then the two wirings assembled from those pieces.


def combine_pair(v: Verdicts, a: str, b: str) -> list[EdgeStats]:
    """Signal-level accuracy of every way two directional gates can be combined.

    Signals only: no conviction floor, no payout test, no risk layer, so this
    is what the gate logic itself is worth before anything else thins it.
    """
    graded = v.graded()
    days = v.days
    break_even = v.break_even
    da, db = v.direction[a], v.direction[b]
    fa = v.passed[a] & (da != FLAT)
    fb = v.passed[b] & (db != FLAT)
    a_only = fa & ~fb
    b_only = fb & ~fa
    agree = fa & fb & (da == db)
    disagree = fa & fb & (da != db)
    either = a_only | b_only | agree
    side_either = np.where(fa, da, db).astype(np.int8)

    def stats(label: str, mask: np.ndarray, side: np.ndarray) -> EdgeStats:
        fired = mask
        scored = fired & graded
        wins = int((scored & (side == v.outcome)).sum())
        losses = int((scored & (side == -v.outcome)).sum())
        voids = int((fired & ~graded).sum())
        return EdgeStats(label=label, signals=int(fired.sum()), wins=wins,
                         losses=losses, voids=voids, break_even=break_even,
                         days=days)

    none = np.zeros(v.bars, dtype=np.int8)
    return [
        stats(f"{a} alone", a_only, da),
        stats(f"{b} alone", b_only, db),
        stats("both fire, agree", agree, da),
        stats("both fire, disagree", disagree, none),
        stats("AND: both must agree (unanimous)", agree, da),
        stats("OR: either, no dissent (weighted)", either, side_either),
    ]


def render_combination(rows: Sequence[EdgeStats], a: str, b: str) -> str:
    lines = [f"3b. HOW {a} AND {b} COMBINE -- signal level, before conviction and risk",
             f"   {'cells':<38}{'signals':>9}{'per day':>9}{'accuracy':>10}"
             f"{'vs b/e':>9}{'z':>7}   verdict"]
    for r in rows:
        acc = f"{r.accuracy:.2%}" if r.accuracy is not None else "n/a"
        edge = f"{r.edge:+.2%}" if r.edge is not None else "n/a"
        z = f"{r.z:+.1f}" if r.z is not None else "n/a"
        verdict = "no side to grade" if r.label.endswith("disagree") else r.verdict
        lines.append(f"   {r.label:<38}{r.signals:>9,}{r.per_day:>9.2f}{acc:>10}"
                     f"{edge:>9}{z:>7}   {verdict}")
    lines.append("   AND is betting.mode unanimous; OR is betting.mode weighted with")
    lines.append("   allow_dissent false. Read per day beside accuracy: a wiring that")
    lines.append("   fires too rarely cannot prove itself on any holdout.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 4. leave-one-out
# --------------------------------------------------------------------------- #


@dataclass
class LeaveOneOut:
    removed: str
    signals: int
    per_day: float
    accuracy: float | None
    break_even: float


def leave_one_out(series: BarSeries, cfg: StrategyConfig,
                  reference: BarSeries | None = None) -> list[LeaveOneOut]:
    """Score the stack, then score it again with each gate removed in turn."""
    from .attribution import stack_edge

    fs = build_features(series, cfg, reference)
    rows: list[LeaveOneOut] = []

    full = stack_edge(series, cfg, label="(full stack)", features=fs)
    rows.append(LeaveOneOut("(none)", full.signals, full.per_day, full.accuracy,
                            full.break_even))
    for name in cfg.gate_stack:
        remaining = [g for g in cfg.gate_stack if g != name]
        if len(remaining) < 3:
            continue
        try:
            trimmed = config_from_dict({**_as_dict(cfg), "gate_stack": remaining},
                                       fit_stack=True)
        except ValueError:
            continue
        stats = stack_edge(series, trimmed, label=f"without {name}", features=fs)
        rows.append(LeaveOneOut(name, stats.signals, stats.per_day,
                                stats.accuracy, stats.break_even))
    return rows


def _as_dict(cfg: StrategyConfig) -> dict:
    from .config import to_dict

    return to_dict(cfg)


# --------------------------------------------------------------------------- #
# 5. betting and risk conditions
# --------------------------------------------------------------------------- #


@dataclass
class ConditionBinding:
    name: str
    blocks: int
    sole_blocks: int
    can_ever_bind: bool
    note: str = ""


def condition_bindings(series: BarSeries, cfg: StrategyConfig,
                       reference: BarSeries | None = None
                       ) -> list[ConditionBinding]:
    """Which betting conditions actually refuse bars, and which are dead code.

    A condition that never blocks anything is not a safeguard, it is a comment.
    The usual cause is two conditions expressing the same threshold in different
    units, where one is strictly tighter than the other.
    """
    fs = build_features(series, cfg, reference)
    engine = SignalEngine(cfg)
    counts: dict[str, int] = {}
    sole: dict[str, int] = {}
    for i in range(engine.warmup_bars(fs), len(series) - cfg.betting.horizon_bars):
        sig = engine.evaluate(fs, i)
        for label in sig.blocked_by:
            counts[label] = counts.get(label, 0) + 1
        if len(sig.blocked_by) == 1:
            sole[sig.blocked_by[0]] = sole.get(sig.blocked_by[0], 0) + 1

    implied = cfg.implied_conviction_floor()
    rows = []
    for label in ("gate_agreement", "priced_edge", "timing"):
        note = ""
        can_bind = counts.get(label, 0) > 0
        if label == "timing":
            note = ("not testable on history: a backtest always decides "
                    f"{cfg.betting.latency_seconds}s after the close. Only a live "
                    "or replayed feed can exercise it.")
            can_bind = False
        if label == "priced_edge":
            if implied is None:
                note = "unreachable at any conviction: prob_cap cannot clear the payout"
                can_bind = False
            elif cfg.betting.min_conviction >= implied:
                note = (f"never binds: needs conviction < {implied:.3f}, but the "
                        f"conviction floor already demands "
                        f"{cfg.betting.min_conviction:.3f}")
                can_bind = False
            else:
                note = f"binds for conviction in [{cfg.betting.min_conviction:.2f}, {implied:.3f})"
        rows.append(ConditionBinding(label, counts.get(label, 0),
                                     sole.get(label, 0), can_bind, note))
    return rows


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render_structural(rows: Sequence[SharedInputs], gate_names: Sequence[str]) -> str:
    lines = ["1. STRUCTURAL OVERLAP -- do any two gates read the same feature?"]
    if not rows:
        lines.append(f"   None. The {len(gate_names)} gates read disjoint feature "
                     "sets, so nothing here forces them to agree.")
        return "\n".join(lines)
    lines.append(f"   {'pair':<42}{'shared':>7}  features")
    for r in rows:
        lines.append(f"   {r.a + ' / ' + r.b:<42}{len(r.shared):>7}  "
                     f"{', '.join(r.shared)}")
    return "\n".join(lines)


def render_pairs(rows: Sequence[PairOverlap], limit: int = 12) -> str:
    lines = ["2. VERDICT OVERLAP -- do they pass on the same bars, and agree?",
             f"   {'pair':<42}{'phi':>7}{'jaccard':>9}{'lift':>7}"
             f"{'dir agree':>11}   verdict"]
    for r in rows[:limit]:
        agree = (f"{r.direction_agreement:.0%}" if r.direction_agreement is not None
                 else "n/a")
        lines.append(f"   {r.a + ' / ' + r.b:<42}{r.phi:>+7.2f}{r.jaccard:>9.2f}"
                     f"{r.lift:>7.2f}{agree:>11}   {r.verdict}")
    lines.append("   phi 0 is independent, 1 identical. lift 1.0 means A passing")
    lines.append("   tells you nothing about whether B passes.")
    return "\n".join(lines)


def render_marginal(rows: Sequence[MarginalValue]) -> str:
    lines = ["3. MARGINAL VALUE -- given the other gates passed, does this one matter?",
             f"   {'gate':<20}{'bars':>8}{'hit if pass':>13}{'hit if fail':>13}"
             f"{'separation':>12}{'z':>7}   verdict"]
    for r in rows:
        hw = f"{r.hit_with:.2%}" if r.hit_with is not None else "n/a"
        ho = f"{r.hit_without:.2%}" if r.hit_without is not None else "n/a"
        sep = f"{r.separation:+.2%}" if r.separation is not None else "n/a"
        z = f"{r.z:+.1f}" if r.z is not None else "n/a"
        lines.append(f"   {r.gate:<20}{r.rest_passes:>8,}{hw:>13}{ho:>13}"
                     f"{sep:>12}{z:>7}   {r.verdict}")
    lines.append("   A gate with no separation is redundant however unique its inputs.")
    return "\n".join(lines)


def render_leave_one_out(rows: Sequence[LeaveOneOut]) -> str:
    lines = ["4. LEAVE-ONE-OUT -- the whole stack without each gate",
             f"   {'stack':<26}{'signals':>9}{'per day':>9}{'accuracy':>10}"
             f"{'vs b/e':>9}"]
    for r in rows:
        acc = f"{r.accuracy:.2%}" if r.accuracy is not None else "n/a"
        edge = (f"{r.accuracy - r.break_even:+.2%}" if r.accuracy is not None
                else "n/a")
        label = "(full stack)" if r.removed == "(none)" else f"without {r.removed}"
        lines.append(f"   {label:<26}{r.signals:>9,}{r.per_day:>9.2f}{acc:>10}"
                     f"{edge:>9}")
    return "\n".join(lines)


def render_conditions(rows: Sequence[ConditionBinding],
                      cfg: StrategyConfig | None = None) -> str:
    lines = ["5. BETTING CONDITIONS -- does each one ever actually refuse a bar?",
             f"   {'condition':<20}{'blocked':>10}{'sole reason':>13}   note"]
    for r in rows:
        lines.append(f"   {r.name:<20}{r.blocks:>10,}{r.sole_blocks:>13,}   "
                     f"{r.note or ('binds' if r.can_ever_bind else 'never binds')}")
    if cfg is not None:
        floor = cfg.effective_conviction_floor()
        lines.append("   Conditions 1 and 2 are two thresholds on one scalar, so there is")
        lines.append(f"   really one: conviction >= {floor:.3f}, set by "
                     f"{cfg.binding_betting_condition()}. Both are kept because")
        lines.append("   which one binds moves with the payout, not because they are")
        lines.append("   independent safeguards.")
    return "\n".join(lines)


def full_report(series: BarSeries, cfg: StrategyConfig,
                reference: BarSeries | None = None) -> str:
    """Everything above, for the configured stack."""
    fs = build_features(series, cfg, reference)
    stack = [g.name for g in build_stack(cfg.gate_stack)]
    verdicts = collect_verdicts(series, cfg, stack, features=fs)
    directional = [n for n in stack if not GATE_REGISTRY[n].is_veto]

    days = len(series) / BARS_PER_DAY
    parts = [
        f"OVERLAP ANALYSIS  stack: {', '.join(stack)}",
        f"{len(series):,} bars ({days:.0f} days), "
        f"{verdicts.bars:,} evaluated after warm-up",
        "",
        render_structural(structural_overlap(stack), stack),
        "",
        render_pairs(verdict_overlap(verdicts)),
        "",
        render_marginal(marginal_value(verdicts, directional)),
        "",
    ]
    two = [n for n in stack if GATE_REGISTRY[n].is_directional]
    if len(two) == 2:
        parts += [render_combination(combine_pair(verdicts, *two), *two), ""]
    parts += [
        render_leave_one_out(leave_one_out(series, cfg, reference)),
        "",
        render_conditions(condition_bindings(series, cfg, reference), cfg),
    ]
    return "\n".join(parts)
