"""Gate aggregation and the three conditions that must hold to place a bet.

The gates decide whether the market is worth an opinion.  These three
conditions decide whether that opinion is worth money:

1. **Agreement** -- every veto gate passes, the directional gates point the same
   way, and the blended conviction clears a floor.
2. **Priced edge** -- the modelled probability beats the break-even probability
   implied by the payout, by a required margin.  A 56%-accurate signal is a
   losing bet at 1.80x and a good one at 1.95x; only this condition knows the
   difference.
3. **Timing** -- the signal is fresh and there is enough time left before the
   contract expires for the move to actually happen.

A binary contract has no stop loss and no exit: the only two decisions are
whether to bet and how much.  These conditions own the first one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import StrategyConfig
from .features import FeatureSet
from .gates import DOWN, FLAT, UP, Check, Gate, GateResult, build_stack, direction_name


@dataclass
class Signal:
    """The full, auditable decision for one bar."""

    bar_index: int
    ts: int
    price: float
    side: int
    conviction: float
    p_model: float
    break_even: float
    edge: float
    expected_value: float
    gate_results: tuple[GateResult, ...] = ()
    conditions: tuple[Check, ...] = ()
    tradable: bool = False
    blocked_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def side_name(self) -> str:
        return direction_name(self.side)

    @property
    def answer(self) -> str:
        """The yes/no the strategy was asked for."""
        return "YES" if self.tradable else "NO"

    def brief(self) -> str:
        """One line: the yes/no answer and, if no, the single reason.

        The whole bet is one bit, decided in the seconds after a candle closes.
        Everything else in this module exists to justify this line.
        """
        if self.tradable:
            return (f"{self.side_name} - p {self.p_model:.3f} - "
                    f"conviction {self.conviction:.2f}")
        reason = self._first_reason()
        return f"NO BET - {reason}"

    def _first_reason(self) -> str:
        """The earliest thing that stopped the bar, named as a person would."""
        for gate in self.gate_results:
            if not gate.passed:
                failed = gate.failed_checks
                return (f"{gate.name}: {failed[0].label}" if failed else gate.name)
        for condition in self.conditions:
            if condition.applicable and not condition.passed:
                return condition.label
        return "no side proposed"

    def report(self) -> str:
        """Multi-line trace: every gate, then every betting condition."""
        lines = [
            f"bar {self.bar_index}  price {self.price:,.2f}",
            "gates:",
        ]
        for g in self.gate_results:
            lines.append(f"  {g.summary()}")
            for c in g.checks:
                lines.append(f"      {c}")
        lines.append("betting conditions:")
        for c in self.conditions:
            lines.append(f"  {c}")
        lines.append(
            f"=> {self.answer}"
            + (f" / {self.side_name}" if self.side != FLAT else "")
            + f"  conviction {self.conviction:.2f}"
            f"  p_model {self.p_model:.3f}"
            f"  break-even {self.break_even:.3f}"
            f"  edge {self.edge:+.3f}"
        )
        if self.blocked_by:
            lines.append(f"   blocked by: {', '.join(self.blocked_by)}")
        return "\n".join(lines)


class SignalEngine:
    """Runs the gate stack, then the three betting conditions, for one bar."""

    def __init__(self, cfg: StrategyConfig):
        cfg.validate()
        self.cfg = cfg
        self.stack: list[Gate] = build_stack(cfg.gate_stack)
        self.veto_gates = [g for g in self.stack if g.is_veto]
        self.directional_gates = [g for g in self.stack if g.is_directional]
        self.confirm_gates = [g for g in self.stack if g.is_confirm]
        self.break_even = cfg.break_even_probability()
        self.odds = cfg.payoff_odds()

    # ------------------------------------------------------------------ #

    @property
    def required_features(self) -> tuple[str, ...]:
        names: list[str] = []
        for g in self.stack:
            names.extend(g.requires)
        return tuple(dict.fromkeys(names))

    def warmup_bars(self, fs: FeatureSet) -> int:
        return fs.warmup_bars(self.required_features)

    def weight(self, gate_name: str) -> float:
        return float(self.cfg.betting.gate_weights.get(gate_name, 1.0))

    # ------------------------------------------------------------------ #

    def evaluate(self, fs: FeatureSet, i: int, now_ts: int | None = None) -> Signal:
        b = self.cfg.betting
        ts = int(fs.series.ts[i])
        price = float(fs.series.close[i])
        if now_ts is None:
            now_ts = ts + b.latency_seconds

        # Two passes: the confirmation gates need the side the directional
        # gates chose, so they cannot run until that side exists.
        proposed_results = tuple(g.evaluate(fs, i, self.cfg.gates)
                                 for g in self.veto_gates + self.directional_gates)
        proposed, _, _ = self._aggregate(proposed_results, ())
        confirm_results = tuple(
            g.evaluate(fs, i, self.cfg.gates, proposed) for g in self.confirm_gates)
        results = proposed_results + confirm_results
        side, conviction, agreement = self._aggregate(proposed_results, confirm_results)

        p_model = self._probability(conviction) if side != FLAT else 0.5
        edge = p_model - self.break_even
        ev = p_model * self.odds - (1.0 - p_model)

        has_side = side != FLAT
        conditions = [agreement,
                      self._edge_condition(p_model, edge, applicable=has_side),
                      self._timing_condition(ts, now_ts)]
        tradable = has_side and all(c.passed for c in conditions if c.applicable)
        blocked = tuple(c.label for c in conditions
                        if c.applicable and not c.passed)

        return Signal(
            bar_index=i, ts=ts, price=price,
            side=side if tradable else side,
            conviction=conviction, p_model=p_model,
            break_even=self.break_even, edge=edge, expected_value=ev,
            gate_results=results, conditions=tuple(conditions),
            tradable=tradable, blocked_by=blocked,
        )

    # -- condition 1 ---------------------------------------------------- #

    def _aggregate(self, results: tuple[GateResult, ...],
                   confirmations: tuple[GateResult, ...]) -> tuple[int, float, Check]:
        """Blend gate verdicts into a side, a conviction and condition 1.

        Called twice per bar: once with no confirmations, to propose a side, and
        once with them, to reach the final verdict.
        """
        b = self.cfg.betting
        failed_vetoes = [r.name for r in results if r.is_veto and not r.passed]
        if failed_vetoes:
            return FLAT, 0.0, Check(
                "gate_agreement", False,
                f"veto gate(s) failed: {', '.join(failed_vetoes)}")

        directional = [r for r in results if r.is_directional]
        passing = [r for r in directional if r.passed and r.direction != FLAT]
        failing = [r.name for r in directional if not r.passed]

        if b.mode == "unanimous":
            if failing:
                return FLAT, 0.0, Check(
                    "gate_agreement", False,
                    f"gate(s) not confirming: {', '.join(failing)}")
            sides = {r.direction for r in passing}
            if len(sides) != 1:
                votes = ", ".join(f"{r.name}->{direction_name(r.direction)}"
                                  for r in passing)
                return FLAT, 0.0, Check("gate_agreement", False,
                                        f"gates disagree on direction ({votes})")
            side = sides.pop()
        else:
            score_up = sum(self.weight(r.name) * r.score
                           for r in passing if r.direction == UP)
            score_down = sum(self.weight(r.name) * r.score
                             for r in passing if r.direction == DOWN)
            if score_up == score_down:
                return FLAT, 0.0, Check("gate_agreement", False,
                                        "weighted vote is a tie")
            side = UP if score_up > score_down else DOWN
            dissent = [r.name for r in passing if r.direction == -side]
            if dissent and not b.allow_dissent:
                return FLAT, 0.0, Check(
                    "gate_agreement", False,
                    f"gate(s) voting the other way: {', '.join(dissent)}")

        agreeing = [r for r in passing if r.direction == side]
        if len(agreeing) < b.min_directional_gates:
            return FLAT, 0.0, Check(
                "gate_agreement", False,
                f"{len(agreeing)} confirming gate(s) < "
                f"{b.min_directional_gates} required")

        refused = [r.name for r in confirmations if not r.passed]
        if refused:
            return FLAT, 0.0, Check(
                "gate_agreement", False,
                f"confirmation gate(s) refused {direction_name(side)}: "
                f"{', '.join(refused)}")

        scoring = agreeing + [r for r in confirmations if r.passed]
        total_weight = sum(self.weight(r.name) for r in scoring)
        conviction = (sum(self.weight(r.name) * r.score for r in scoring)
                      / total_weight) if total_weight else 0.0
        ok = conviction >= b.min_conviction
        check = Check(
            "gate_agreement", ok,
            f"{len(agreeing)}/{len(directional)} directional gates confirm "
            f"{direction_name(side)}"
            + (f", {len(confirmations)} confirmation gate(s) agree"
               if confirmations else "")
            + f", conviction {conviction:.2f} vs floor {b.min_conviction}")
        return (side if ok else FLAT), conviction, check

    # -- condition 2 ---------------------------------------------------- #

    def _probability(self, conviction: float) -> float:
        """Map conviction onto a win probability.

        Deliberately a plain, monotone map rather than a fitted model: the
        backtest's calibration table is what tells you whether ``prob_cap`` is
        honest for your data.  Start conservative and raise it only once the
        realised hit rate per conviction bucket supports it.
        """
        b = self.cfg.betting
        c = min(1.0, max(0.0, conviction)) ** max(1e-9, b.prob_curve)
        return 0.5 + (b.prob_cap - 0.5) * c

    def _edge_condition(self, p_model: float, edge: float,
                        applicable: bool = True) -> Check:
        b = self.cfg.betting
        if not applicable:
            return Check("priced_edge", False,
                         "no side proposed, so there is no edge to price",
                         applicable=False)
        return Check(
            "priced_edge", edge >= b.required_edge,
            f"p_model {p_model:.3f} - break-even {self.break_even:.3f} = "
            f"{edge:+.3f} vs required {b.required_edge:+.3f} "
            f"(payout {self.odds:.3f}x)")

    # -- condition 3 ---------------------------------------------------- #

    def _timing_condition(self, bar_ts: int, now_ts: int) -> Check:
        b = self.cfg.betting
        age = now_ts - bar_ts
        expiry = bar_ts + b.horizon_bars * b.bar_seconds
        remaining = expiry - now_ts
        ok = 0 <= age <= b.max_signal_age_seconds and remaining >= b.min_seconds_to_expiry
        return Check(
            "timing", ok,
            f"signal age {age}s <= {b.max_signal_age_seconds}s and "
            f"{remaining}s to expiry >= {b.min_seconds_to_expiry}s")
