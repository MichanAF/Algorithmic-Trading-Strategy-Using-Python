"""The overlay policy: one number, and the reasons behind it.

The strategy's only moving part is the hedge ratio ``h`` -- the fraction of the
core that is short a perp against itself.

    h = 0.00   fully exposed. You own the core outright.
    h = 0.50   half hedged. Half the beta, half the funding.
    h = 1.00   delta neutral. No price exposure; funding is the whole return.

Two independent reasons move it, and they are kept independent on purpose:

* **Carry** -- funding is rich, so you are being paid to be short. This is a
  yield decision. It says nothing about where price is going.
* **Risk** -- the trend broke, volatility spiked, or the core is deep in
  drawdown. This is an insurance decision. It does not care what funding pays.

Folding them into one score would let a rich-funding reading cancel a
trend-break reading, which is exactly backwards: those are the conditions where
you want to be hedged *twice over*, not netted to flat. The default
``combine="max"`` lets either reason call for a hedge on its own.

Three guards then stand between the target and the order book, because a policy
that recomputes ``h`` every hour and trades the difference will pay 0.07% a
round trip to chase noise:

* **the ladder** -- move at most one step toward the target per decision
* **the minimum step** -- ignore adjustments too small to be worth the fee
* **the minimum hold** -- do not close a carry hedge before it has paid for
  itself, unless funding flipped or the risk signal took over
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .carry import quote_carry
from .checks import Check, blockers, render
from .config import StrategyConfig
from .venue import MetaMaskVenue


def _days(x: float) -> str:
    """Format a hold length. Infinity is "never", not "inf"."""
    if x == float("inf"):
        return "never"
    return "immediate" if x <= 0.0 else f"{x:.1f}d"


def _ramp(x: float, lo: float, hi: float) -> float:
    """Linear 0..1 ramp. Below ``lo`` is 0, above ``hi`` is 1."""
    if hi <= lo:
        raise ValueError("ramp needs hi > lo")
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


@dataclass(frozen=True)
class MarketState:
    """Everything the policy is allowed to look at.

    Deliberately small. A hedge ratio that depends on nine indicators is a
    hedge ratio nobody can explain at the moment it matters.
    """

    funding_apr: float = 0.0            # trailing average over the lookback
    funding_now_apr: float = 0.0        # most recent hourly reading, annualised
    negative_funding_hours: int = 0     # consecutive hours of longs being paid
    trend_score: float = 0.0            # -1 fully broken .. +1 fully intact
    vol_rank: float = 0.5               # 0..1 percentile of realised vol
    core_drawdown: float = 0.0          # 0..1 from the core's peak
    current_ratio: float = 0.0          # the hedge you have on right now
    days_held: float = 0.0              # age of the current hedge
    core_usd: float = 0.0
    # Set while a forced deleverage is still cooling off.  Blocks *increases*
    # only -- a risk signal may always cut further, it just may not re-short
    # into the move that trimmed it.
    cooldown_active: bool = False


@dataclass
class HedgeDecision:
    target_ratio: float
    applied_ratio: float
    action: str                         # "increase" | "reduce" | "hold"
    delta_ratio: float
    delta_notional_usd: float
    est_cost_usd: float
    carry_component: float
    risk_component: float
    checks: tuple[Check, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked_by(self) -> tuple[str, ...]:
        return blockers(self.checks)

    def report(self) -> str:
        lines = ["hedge policy:", render(self.checks)]
        lines.append(
            f"=> carry {self.carry_component:.2f} / risk {self.risk_component:.2f} "
            f"-> target h={self.target_ratio:.2f}, applying h={self.applied_ratio:.2f} "
            f"({self.action.upper()} {self.delta_ratio:+.2f})")
        if abs(self.delta_notional_usd) > 0.0:
            lines.append(
                f"   trade {abs(self.delta_notional_usd):,.0f} USD of perp notional, "
                f"est. cost {self.est_cost_usd:,.2f} USD")
        if self.reasons:
            lines.extend(f"   {r}" for r in self.reasons)
        return "\n".join(lines)


def carry_component(state: MarketState, cfg: StrategyConfig) -> tuple[float, Check]:
    """How much hedge the funding regime alone justifies.

    Ramps from ``enter_apr`` to ``full_apr``. Baseline funding is about 11% APR,
    which is why ``enter_apr`` defaults to 15% rather than to zero: being paid
    the interest component is not the same as being paid a premium, and hedging
    for 11% while forgoing the core's beta is a decision, not a free lunch.

    Below ``exit_apr`` the component is zero outright. The gap between
    ``exit_apr`` and ``enter_apr`` is hysteresis -- without it the hedge
    oscillates across the threshold and pays the round trip each time.
    """
    c = cfg.carry
    apr = state.funding_apr
    if apr < c.exit_apr:
        return 0.0, Check("carry_signal", True,
                          f"funding {apr:.2%} APR below exit {c.exit_apr:.2%}: "
                          f"carry wants no hedge")
    value = _ramp(apr, c.enter_apr, c.full_apr)
    detail = (f"funding {apr:.2%} APR on a {c.lookback_hours}h average "
              f"(ramp {c.enter_apr:.0%}->{c.full_apr:.0%}) -> {value:.2f}")
    if c.exit_apr <= apr < c.enter_apr:
        detail += "; in the hysteresis band, holding whatever is already on"
    return value, Check("carry_signal", True, detail)


def risk_component(state: MarketState, cfg: StrategyConfig) -> tuple[float, Check]:
    """How much hedge the risk picture alone justifies, funding ignored.

    A volatility shock is treated as a floor rather than a term: when realised
    vol is in its top decile the strategy wants a material hedge on regardless
    of what the trend score says, because that is precisely the regime where
    the trend score is about to be wrong.
    """
    r = cfg.risk
    broken = max(0.0, -state.trend_score)
    dd = min(1.0, state.core_drawdown / r.max_core_drawdown_pct) if r.max_core_drawdown_pct else 0.0
    value = r.trend_break_weight * broken + r.drawdown_weight * dd

    parts = [f"trend {state.trend_score:+.2f} -> {broken:.2f}",
             f"drawdown {state.core_drawdown:.1%}/{r.max_core_drawdown_pct:.0%} -> {dd:.2f}"]
    if state.vol_rank >= r.vol_shock_rank:
        floor = 0.50
        parts.append(f"vol rank {state.vol_rank:.2f} >= {r.vol_shock_rank:.2f}: "
                     f"shock floor {floor:.2f}")
        value = max(value, floor)
    value = max(0.0, min(1.0, value))
    return value, Check("risk_signal", True, "; ".join(parts) + f" -> {value:.2f}")


def decide(state: MarketState, cfg: StrategyConfig,
           venue: MetaMaskVenue | None = None) -> HedgeDecision:
    """Combine the two signals, apply the three guards, return one trade."""
    venue = venue or cfg.venue
    h, c = cfg.hedge, cfg.carry
    checks: list[Check] = []
    reasons: list[str] = []

    carry, carry_check = carry_component(state, cfg)
    risk, risk_check = risk_component(state, cfg)
    checks += [carry_check, risk_check]

    if h.combine == "max":
        raw = max(h.carry_weight * carry, h.risk_weight * risk)
    elif h.combine == "sum":
        raw = h.carry_weight * carry + h.risk_weight * risk
    else:
        raw = (h.carry_weight * carry + h.risk_weight * risk) / 2.0

    span = h.max_hedge_ratio - h.min_hedge_ratio
    target = h.min_hedge_ratio + max(0.0, min(1.0, raw)) * span
    checks.append(Check("target_ratio", True,
                        f"combine={h.combine} -> raw {raw:.2f} -> "
                        f"h={target:.2f} within [{h.min_hedge_ratio:.2f}, "
                        f"{h.max_hedge_ratio:.2f}]"))

    # -- guard 1: funding flipped -------------------------------------- #
    flipped = state.negative_funding_hours >= c.flip_exit_hours
    checks.append(Check(
        "funding_flip", not flipped,
        f"{state.negative_funding_hours}h of negative funding vs "
        f"{c.flip_exit_hours}h limit"
        + ("; the carry premise is gone, unwinding the carry portion" if flipped else ""),
        applicable=state.current_ratio > 0.0 or carry > 0.0))
    if flipped:
        target = min(target, h.min_hedge_ratio + risk * span)
        reasons.append("funding flipped: carry portion released, risk portion kept")

    # -- guard 2: minimum hold ----------------------------------------- #
    # The guard only binds while the trade it is protecting still makes sense.
    # A hedge whose funding has decayed below `exit_apr` is no longer earning
    # anything to amortise, so holding it to "reach break-even" would pin a
    # dead position open forever waiting for a break-even that cannot arrive.
    # Funding does not have to go negative to end a carry trade; it only has to
    # stop paying.
    reducing = target < state.current_ratio
    quote = quote_carry(venue, max(state.funding_apr, 0.0), leverage=h.leverage,
                        from_cash=False, maker=h.use_maker_orders,
                        safety_multiple=c.hold_safety_multiple,
                        floor_days=c.min_hold_days)
    premise_alive = state.funding_apr >= c.exit_apr
    aged_out = state.days_held >= c.max_hold_days
    too_young = (reducing and state.current_ratio > 0.0
                 and state.days_held < quote.min_hold_days
                 and premise_alive and not flipped and not aged_out
                 and risk <= state.current_ratio)
    detail = (f"held {state.days_held:.1f}d vs minimum {_days(quote.min_hold_days)} "
              f"(break-even {_days(quote.break_even_days)} at "
              f"{state.funding_apr:.1%} APR)")
    if not premise_alive:
        detail += (f"; funding below exit {c.exit_apr:.0%}, the premise is gone "
                   f"so the guard does not bind")
    elif aged_out:
        detail += f"; past max_hold_days {c.max_hold_days:.0f}d"
    checks.append(Check(
        "minimum_hold", not too_young, detail,
        applicable=reducing and state.current_ratio > 0.0))
    if too_young:
        target = state.current_ratio
        reasons.append("held below break-even and nothing forced the exit: staying put")

    # -- guard 2b: post-deleverage cooldown ----------------------------- #
    increasing = target > state.current_ratio
    checks.append(Check(
        "deleverage_cooldown", not (state.cooldown_active and increasing),
        f"cooling off after a forced trim for "
        f"{cfg.risk.deleverage_cooldown_hours}h"
        + ("; refusing to rebuild the hedge into the same squeeze"
           if state.cooldown_active and increasing else ""),
        applicable=state.cooldown_active))
    if state.cooldown_active and increasing:
        target = state.current_ratio
        reasons.append("post-deleverage cooldown: cuts allowed, rebuilds are not")

    # -- guard 3: ladder and minimum step ------------------------------ #
    step_cap = span / h.ladder_steps if h.ladder_steps else span
    delta = target - state.current_ratio
    laddered = max(-step_cap, min(step_cap, delta))
    applied = state.current_ratio + laddered
    checks.append(Check("ladder", True,
                        f"step capped at {step_cap:.2f} "
                        f"({h.ladder_steps} steps across the band); "
                        f"{delta:+.2f} -> {laddered:+.2f}"))

    move = applied - state.current_ratio
    big_enough = abs(move) >= h.min_step
    checks.append(Check("minimum_step", big_enough,
                        f"|{move:+.2f}| vs minimum {h.min_step:.2f}"
                        + ("" if big_enough else ": not worth the round trip"),
                        applicable=abs(move) > 0.0))
    if not big_enough:
        applied, move = state.current_ratio, 0.0

    notional = abs(move) * state.core_usd
    cost = notional * venue.perp_cost_pct(maker=h.use_maker_orders)
    action = "hold" if move == 0.0 else ("increase" if move > 0.0 else "reduce")
    if h.use_maker_orders and notional > 0.0:
        reasons.append(
            f"quote as maker: taker would cost "
            f"{notional * venue.perp_taker_pct:,.2f} USD instead of "
            f"{cost:,.2f} USD on this clip")

    return HedgeDecision(
        target_ratio=target, applied_ratio=applied, action=action,
        delta_ratio=move, delta_notional_usd=move * state.core_usd,
        est_cost_usd=cost, carry_component=carry, risk_component=risk,
        checks=tuple(checks), reasons=tuple(reasons),
    )
