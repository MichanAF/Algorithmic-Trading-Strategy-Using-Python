"""What the hedge has to survive, and what you do before it doesn't.

On a cross-margin venue this module would barely need to exist: the spot leg
would collateralise the short, and a rally that hurt one would be paid for by
the other. **MetaMask Perps is isolated margin only**, so it does not work that
way. The two legs are strangers. The short can die of a move the spot leg is
profiting from.

That makes the margin ladder the most important thing here. It converts the
abstract "keep an eye on your margin" into specific prices with specific dollar
amounts attached, computed before the position is opened, so that the decision
during a squeeze is a transfer someone already sized rather than a judgement
call made at speed.

The ladder's arithmetic, for a short of notional ``N`` opened at price ``P``
with collateral ``M``, after an adverse move ``r``:

    loss          = N * r
    equity        = M - N * r
    notional now  = N * (1 + r)
    to survive a further move ``s`` you need equity >= (s(1+m) + m) * N(1+r)

The gap between what you have and that requirement is the top-up. Everything
else in this module is a condition that reports whether some number is on the
right side of a line, in the same auditable form the gates use next door.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .checks import Check, all_passed, blockers, render
from .config import StrategyConfig
from .sizing import Allocation, liquidation_move, margin_fraction_for_survival


@dataclass(frozen=True)
class LadderRung:
    label: str
    adverse_move: float          # from the hedge's entry price
    price: float | None
    equity_usd: float
    equity_pct_of_posted: float
    top_up_usd: float
    action: str

    def __str__(self) -> str:
        px = f"{self.price:,.0f}" if self.price is not None else "--"
        return (f"{self.label:<14} {self.adverse_move:>+7.1%}  {px:>11}  "
                f"{self.equity_usd:>11,.0f} ({self.equity_pct_of_posted:>5.0%})  "
                f"{self.top_up_usd:>10,.0f}  {self.action}")


@dataclass
class MarginLadder:
    notional_usd: float
    posted_usd: float
    reserve_usd: float
    maintenance: float
    entry_price: float | None
    liquidation_move: float              # on posted collateral alone
    liquidation_with_reserve: float      # if the reserve is transferred in time
    ceiling_move: float                  # the move this book was sized to reach
    rungs: tuple[LadderRung, ...]

    def report(self) -> str:
        px = f" from {self.entry_price:,.0f}" if self.entry_price else ""
        ceiling_px = (f" ({self.entry_price * (1.0 + self.ceiling_move):,.0f})"
                      if self.entry_price else "")
        lines = [
            f"margin ladder -- {self.notional_usd:,.0f} short{px}, "
            f"{self.posted_usd:,.0f} posted, {self.reserve_usd:,.0f} staged",
            f"  liquidation {self.liquidation_move:+.1%} on posted collateral, "
            f"{self.liquidation_with_reserve:+.1%} if the reserve lands in time",
            f"  sized to reach {self.ceiling_move:+.1%}{ceiling_px} "
            f"(maintenance {self.maintenance:.2%})",
            f"  {'rung':<14} {'move':>7}  {'price':>11}  {'equity':>19}  "
            f"{'top-up':>10}  action",
        ]
        lines.extend(f"  {r}" for r in self.rungs)
        lines.append(
            "  top-up = the transfer that carries the short to the planned "
            "ceiling from that price,")
        lines.append(
            "  not one that rebuilds a fresh buffer -- a buffer measured from "
            "each new high can never be held.")
        return "\n".join(lines)


def build_ladder(cfg: StrategyConfig, notional_usd: float, posted_usd: float,
                 reserve_usd: float, maintenance: float | None = None,
                 entry_price: float | None = None) -> MarginLadder:
    """Price the rungs before you need them.

    Two liquidation points matter and they are not the same number. The first
    is where the short dies on the collateral currently posted. The second is
    where it dies once the staged reserve has been moved across. The gap
    between them is the entire value of the reserve, and it is only real if the
    transfer actually happens -- which is why ``reserve_on_arbitrum`` is a risk
    condition and not a preference.

    ``top_up_usd`` is the transfer that carries the position to the **planned
    ceiling** from that rung's price. It is deliberately not "restore the
    original survival distance from here": that quantity grows every time price
    rises, so chasing it means topping up forever into a move that is going
    against you. The plan has a ceiling. The ladder funds the plan.
    """
    if notional_usd < 0.0 or posted_usd < 0.0:
        raise ValueError("notional and posted collateral must be non-negative")
    m = cfg.blended_maintenance_margin() if maintenance is None else maintenance
    ceiling = cfg.risk.survive_rally_pct

    if notional_usd == 0.0:
        return MarginLadder(0.0, posted_usd, reserve_usd, m, entry_price,
                            float("inf"), float("inf"), ceiling, ())

    lev = notional_usd / posted_usd if posted_usd > 0.0 else float("inf")
    liq = liquidation_move(lev, m) if posted_usd > 0.0 else 0.0
    backed = posted_usd + reserve_usd
    liq_reserved = (liquidation_move(notional_usd / backed, m)
                    if backed > 0.0 else 0.0)

    def rung(label: str, move: float, action: str) -> LadderRung:
        equity = posted_usd - notional_usd * move
        live_notional = notional_usd * (1.0 + move)
        # Remaining distance from this price up to the planned ceiling.
        remaining = max(0.0, (ceiling - move) / (1.0 + move))
        needed = margin_fraction_for_survival(remaining, m) * live_notional
        return LadderRung(
            label=label, adverse_move=move,
            price=entry_price * (1.0 + move) if entry_price else None,
            equity_usd=equity,
            equity_pct_of_posted=equity / posted_usd if posted_usd else 0.0,
            top_up_usd=max(0.0, needed - equity), action=action)

    r = cfg.risk
    rungs = [
        rung("watch", liq * r.margin_call_at,
             f"{r.margin_call_at:.0%} of the buffer gone -- confirm the reserve "
             f"is liquid and funding still pays"),
        rung("top up", liq * r.margin_urgent_at,
             "transfer from the Arbitrum reserve now"),
        rung("liq (posted)", liq,
             "short dies here on posted collateral alone"),
        rung("liq (reserved)", liq_reserved,
             "short dies here even after the reserve is moved across"),
        rung("ceiling", ceiling,
             "the move this book was sized to reach; re-plan at or before this"),
    ]
    return MarginLadder(notional_usd, posted_usd, reserve_usd, m, entry_price,
                        liq, liq_reserved, ceiling,
                        tuple(sorted(rungs, key=lambda x: x.adverse_move)))


@dataclass
class RiskReport:
    checks: tuple[Check, ...]
    ladder: MarginLadder | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all_passed(self.checks)

    @property
    def blocked_by(self) -> tuple[str, ...]:
        return blockers(self.checks)

    def report(self) -> str:
        lines = ["risk conditions:", render(self.checks)]
        lines.append(f"=> {'OK' if self.ok else 'ATTENTION'}"
                     + (f"  ({', '.join(self.blocked_by)})" if not self.ok else ""))
        if self.ladder is not None and self.ladder.notional_usd > 0.0:
            lines += ["", self.ladder.report()]
        if self.notes:
            lines += [""] + [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def assess(cfg: StrategyConfig, alloc: Allocation, *, hedge_ratio: float,
           adverse_move_so_far: float = 0.0, funding_apr: float = 0.0,
           negative_funding_hours: int = 0, core_drawdown: float = 0.0,
           entry_price: float | None = None) -> RiskReport:
    """Every risk condition, on a live book."""
    r, venue = cfg.risk, cfg.venue
    m = cfg.blended_maintenance_margin()
    notional = alloc.hedge_notional(hedge_ratio)
    posted = alloc.margin_required(hedge_ratio)
    checks: list[Check] = []
    notes: list[str] = []

    # -- 1. can the short survive the move it was sized for? ------------- #
    lev = cfg.hedge.leverage
    bare_liq = liquidation_move(lev, m)
    needed_frac = margin_fraction_for_survival(r.survive_rally_pct, m)
    total_available = posted + alloc.reserve_usd
    survives = total_available >= needed_frac * notional - 1e-9
    checks.append(Check(
        "survival_margin", survives or notional == 0.0,
        f"{total_available:,.0f} available vs {needed_frac * notional:,.0f} needed "
        f"to survive {r.survive_rally_pct:+.0%} "
        f"({lev:g}x posted alone liquidates at {bare_liq:+.1%})",
        applicable=notional > 0.0))

    # -- 2. is the reserve actually reachable? --------------------------- #
    checks.append(Check(
        "reserve_staged", r.reserve_on_arbitrum or alloc.reserve_usd == 0.0,
        (f"{alloc.reserve_usd:,.0f} staged as USDC on "
         f"{venue.perp_withdrawal_chain}, one transaction from the perp account"
         if r.reserve_on_arbitrum else
         f"{alloc.reserve_usd:,.0f} reserve is NOT staged on "
         f"{venue.perp_withdrawal_chain}; a bridge hop during a squeeze is the "
         f"one transfer that will not arrive in time"),
        applicable=alloc.reserve_usd > 0.0))

    # -- 3. how much of the buffer is already spent? --------------------- #
    spent = adverse_move_so_far / bare_liq if bare_liq > 0.0 else 0.0
    checks.append(Check(
        "buffer_consumed", spent < r.margin_urgent_at,
        f"{adverse_move_so_far:+.1%} of a {bare_liq:+.1%} buffer = {spent:.0%} spent "
        f"(watch {r.margin_call_at:.0%}, top up {r.margin_urgent_at:.0%})",
        applicable=notional > 0.0 and adverse_move_so_far > 0.0))

    # -- 4. is the carry premise still alive? ---------------------------- #
    checks.append(Check(
        "funding_regime", negative_funding_hours < cfg.carry.flip_exit_hours,
        f"funding {funding_apr:.2%} APR, {negative_funding_hours}h negative "
        f"(limit {cfg.carry.flip_exit_hours}h)",
        applicable=hedge_ratio > 0.0))

    # -- 5. core drawdown ------------------------------------------------ #
    checks.append(Check(
        "core_drawdown", core_drawdown < r.max_core_drawdown_pct,
        f"core down {core_drawdown:.1%} vs {r.max_core_drawdown_pct:.0%} limit",
        applicable=core_drawdown > 0.0))

    # -- 6. concentration ------------------------------------------------ #
    worst = max(cfg.core.weights.items(), key=lambda kv: kv[1], default=("", 0.0))
    checks.append(Check(
        "concentration", worst[1] <= r.max_single_asset_weight,
        f"largest core weight {worst[0]} {worst[1]:.0%} vs "
        f"{r.max_single_asset_weight:.0%} limit"))

    # -- 7. liquidity: can you actually sell the core if you must? ------- #
    liquid = alloc.core_liquid_pct()
    checks.append(Check(
        "core_liquidity", liquid >= r.min_core_liquid_pct,
        f"{liquid:.0%} of core is liquid vs {r.min_core_liquid_pct:.0%} minimum "
        f"({alloc.staked_usd:,.0f} staked)",
        applicable=alloc.core_usd > 0.0))

    # -- 8. basis: are you hedging with the wrong instrument? ------------ #
    proxies = {s: h for s, h in cfg.hedged_symbols().items() if s != h}
    checks.append(Check(
        "basis_risk", not proxies,
        (f"proxy hedges in use: {proxies}; these are correlation bets, not "
         f"hedges, and they fail exactly when correlations do"
         if proxies else "every core asset is hedged with its own perp"),
        applicable=bool(proxies)))

    if venue.isolated_margin_only and notional > 0.0:
        notes.append(
            "isolated margin: the core's gain in a rally does NOT back this "
            "short. The ladder below is the only thing between a squeeze and an "
            "unhedged book at the highs.")
    if notional > 0.0 and notional < venue.perp_min_funding_usd * 10:
        notes.append(
            f"at {notional:,.0f} notional the ${venue.perp_withdrawal_fee_usd:.0f} "
            f"withdrawal fee is "
            f"{venue.perp_withdrawal_fee_usd / notional:.2%} of the position")

    ladder = build_ladder(cfg, notional, posted, alloc.reserve_usd, m, entry_price)
    return RiskReport(tuple(checks), ladder, tuple(notes))
