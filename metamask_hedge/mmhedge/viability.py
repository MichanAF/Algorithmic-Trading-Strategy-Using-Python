"""Is your account big enough to run this at all?

The strategy has two kinds of cost, and they behave completely differently as
the account shrinks.

**Proportional costs** scale with the book and never go away: the collateral
parked on HyperCore earns nothing while the same dollars would earn ~4% in
mUSD, which is a drag of roughly 2% of hedged notional a year regardless of
size. Funding has to clear that bar, and at any decent funding rate it does.

**Fixed costs** do not scale: gas on every adjustment and transfer, and the
flat $1 perp withdrawal. These are a rounding error on $25,000 and they are
the entire story on $200. A dozen adjustments a year at fifty cents of gas is
about $6-12 -- call it 4% of a $200 account, against gross carry that cannot
possibly reach that.

So the question "should I run the overlay?" has a different answer at different
sizes, and this module computes where it flips rather than guessing. Two
thresholds bind, and the larger one wins:

1. **Economic** -- the plumbing must not eat more than ``max_overhead_share``
   of the gross carry (default 20%).
2. **Mechanical** -- the smallest ladder clip must clear the venue's minimum
   order size, or the policy simply cannot place the trade it decided on.

Below both, the honest answer is not "run it smaller". It is: hold the spot,
put the rest in the Money Account, and turn the overlay on when the account
has grown into it. Skipping the hedge costs you the volatility reduction; it
does not cost you money you would otherwise have made.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checks import Check, all_passed, blockers, render
from .config import StrategyConfig
from .sizing import allocate, margin_fraction_for_survival


@dataclass(frozen=True)
class OperatingCosts:
    """The per-year plumbing, in transactions rather than percentages.

    Defaults assume an L2 or Solana core where gas is cents, and an operator
    who touches the book monthly. On Ethereum mainnet ``gas_per_tx`` is an
    order of magnitude worse and the thresholds move accordingly.
    """

    gas_per_tx_usd: float = 0.50
    adjustments_per_year: int = 12        # hedge ratio changes
    transfers_per_year: int = 4           # collateral top-ups / sweeps
    withdrawals_per_year: int = 2         # bringing collateral home
    deposits_per_year: int = 0            # DCA top-ups into the core

    def fixed_usd(self, withdrawal_fee_usd: float) -> float:
        txs = (self.adjustments_per_year + self.transfers_per_year
               + self.withdrawals_per_year + self.deposits_per_year)
        return txs * self.gas_per_tx_usd + self.withdrawals_per_year * withdrawal_fee_usd


@dataclass
class ViabilityReport:
    capital_usd: float
    funding_apr: float
    avg_hedge_ratio: float
    core_usd: float
    collateral_usd: float
    gross_carry_usd: float
    collateral_drag_usd: float
    perp_fees_usd: float
    fixed_costs_usd: float
    net_usd: float
    smallest_clip_usd: float
    min_order_usd: float
    economic_minimum_usd: float
    mechanical_minimum_usd: float
    reserve_minimum_usd: float
    checks: tuple[Check, ...] = ()

    @property
    def viable(self) -> bool:
        return all_passed(self.checks)

    @property
    def minimum_usd(self) -> float:
        """The binding threshold: whichever constraint is worse.

        All three must be in here, or the report can contradict itself --
        refusing an account for a reason the headline number does not mention.
        """
        return max(self.economic_minimum_usd, self.mechanical_minimum_usd,
                   self.reserve_minimum_usd)

    @property
    def net_pct(self) -> float:
        return self.net_usd / self.capital_usd if self.capital_usd else 0.0

    @property
    def overhead_share(self) -> float:
        if self.gross_carry_usd <= 0.0:
            return float("inf")
        return self.fixed_costs_usd / self.gross_carry_usd

    def report(self) -> str:
        lines = [
            f"viability at ${self.capital_usd:,.0f} "
            f"(funding {self.funding_apr:.0%} APR, average hedge "
            f"{self.avg_hedge_ratio:.2f})",
            "",
            f"  gross carry            {self.gross_carry_usd:>9,.2f} /yr   "
            f"funding on {self.avg_hedge_ratio * self.core_usd:,.0f} hedged",
            f"  collateral drag        {-self.collateral_drag_usd:>9,.2f} /yr   "
            f"{self.collateral_usd:,.0f} earning 0% instead of mUSD",
            f"  perp fees              {-self.perp_fees_usd:>9,.2f} /yr",
            f"  gas + withdrawals      {-self.fixed_costs_usd:>9,.2f} /yr   "
            f"FIXED -- does not shrink with the account",
            f"  {'-' * 23} {'-' * 9}",
            f"  net carry              {self.net_usd:>9,.2f} /yr   "
            f"({self.net_pct:+.2%} of capital)",
            "",
            f"  plumbing eats {self.overhead_share:.0%} of gross carry",
            f"  smallest ladder clip ${self.smallest_clip_usd:,.2f} "
            f"vs ${self.min_order_usd:,.2f} venue minimum",
            "",
            "conditions:",
            render(self.checks),
            "",
            f"=> {'VIABLE' if self.viable else 'NOT VIABLE'} at this size; "
            f"the overlay needs about ${self.minimum_usd:,.0f}",
        ]
        if not self.viable:
            lines += [
                "",
                "   below the threshold the answer is not 'run it smaller'.",
                "   Hold the spot, keep the rest in the Money Account, and turn",
                "   the overlay on when the account has grown into it. You give",
                "   up the volatility reduction, not money you would have made.",
            ]
        return "\n".join(lines)


def assess_viability(cfg: StrategyConfig, capital_usd: float | None = None, *,
                     funding_apr: float = 0.15, avg_hedge_ratio: float = 0.50,
                     costs: OperatingCosts | None = None,
                     max_overhead_share: float = 0.20,
                     cash_floor_pct: float = 0.10) -> ViabilityReport:
    """Price a year of running the overlay against a year of not running it.

    The baseline is deliberately "same core, collateral in mUSD instead" rather
    than "all cash". That is the real alternative: you were going to own the
    core anyway, and the only question is whether the short is worth its
    plumbing.
    """
    costs = costs or OperatingCosts()
    capital = float(cfg.capital_usd if capital_usd is None else capital_usd)
    if capital <= 0.0:
        raise ValueError("capital_usd must be positive")
    venue = cfg.venue
    alloc = allocate(cfg, capital, cash_floor_pct=cash_floor_pct)

    core_frac = alloc.core_usd / capital
    collateral = alloc.posted_usd + alloc.reserve_usd

    gross = avg_hedge_ratio * alloc.core_usd * funding_apr
    drag = collateral * venue.musd_apy
    clip = alloc.core_usd * cfg.hedge.max_hedge_ratio / max(1, cfg.hedge.ladder_steps)
    fee_rate = abs(venue.perp_maker_pct if cfg.hedge.use_maker_orders
                   else venue.perp_taker_pct)
    perp_fees = costs.adjustments_per_year * clip * fee_rate
    fixed = costs.fixed_usd(venue.perp_withdrawal_fee_usd)
    net = gross - drag - perp_fees - fixed

    # Economic threshold: fixed costs scale as 1/C against carry that scales
    # with C, so the crossover has a closed form.
    per_dollar_carry = avg_hedge_ratio * core_frac * funding_apr
    economic_min = (fixed / (max_overhead_share * per_dollar_carry)
                    if per_dollar_carry > 0.0 else float("inf"))

    # Mechanical threshold: the smallest per-asset ladder clip must be
    # tradeable, since that is the trade the policy actually places.
    smallest_weight = min(cfg.core.weights.values()) if cfg.core.weights else 1.0
    clip_per_dollar = (core_frac * smallest_weight * cfg.hedge.max_hedge_ratio
                       / max(1, cfg.hedge.ladder_steps))
    mechanical_min = (venue.perp_min_order_usd / clip_per_dollar
                      if clip_per_dollar > 0.0 else float("inf"))
    smallest_clip = clip_per_dollar * capital

    # Reserve threshold: a staged reserve smaller than a few withdrawal fees
    # cannot be moved economically, so it is not a reserve.  A book carrying no
    # reserve at all -- leverage at or below the survival leverage, so the full
    # collateral is posted up front -- is exempt, and at small size that is the
    # better structure anyway.
    min_reserve = venue.perp_withdrawal_fee_usd * 5
    reserve_frac = alloc.reserve_usd / capital if capital else 0.0
    reserve_min = (min_reserve / reserve_frac if reserve_frac > 0.0 else 0.0)

    checks = (
        Check("net_carry_positive", net > 0.0,
              f"{net:+,.2f} USD/yr after every cost"),
        Check("overhead_share", gross > 0.0 and fixed <= max_overhead_share * gross,
              f"gas and withdrawals are {fixed:,.2f} of {gross:,.2f} gross carry "
              f"({fixed / gross:.0%} vs {max_overhead_share:.0%} limit)"
              if gross > 0.0 else "no gross carry to measure overhead against"),
        Check("clip_is_tradeable", smallest_clip >= venue.perp_min_order_usd,
              f"smallest ladder clip {smallest_clip:,.2f} vs "
              f"{venue.perp_min_order_usd:,.2f} minimum order"
              + ("" if smallest_clip >= venue.perp_min_order_usd
                 else "; the policy cannot place the trade it decided on")),
        Check("collateral_clears_minimum", collateral >= venue.perp_min_funding_usd,
              f"{collateral:,.2f} collateral vs {venue.perp_min_funding_usd:,.2f} "
              f"minimum funding"),
        Check("reserve_is_meaningful",
              alloc.reserve_usd == 0.0 or alloc.reserve_usd >= min_reserve,
              f"reserve {alloc.reserve_usd:,.2f} vs a "
              f"{venue.perp_withdrawal_fee_usd:,.2f} withdrawal fee"
              + ("" if alloc.reserve_usd == 0.0 or alloc.reserve_usd >= min_reserve
                 else f"; a top-up costs more to send than it delivers. Drop "
                      f"leverage to "
                      f"{1.0 / margin_fraction_for_survival(cfg.risk.survive_rally_pct, cfg.blended_maintenance_margin()):.2f}x "
                      f"or below and post the whole collateral up front instead"),
              applicable=alloc.reserve_usd > 0.0),
    )

    return ViabilityReport(
        capital_usd=capital, funding_apr=funding_apr,
        avg_hedge_ratio=avg_hedge_ratio, core_usd=alloc.core_usd,
        collateral_usd=collateral, gross_carry_usd=gross,
        collateral_drag_usd=drag, perp_fees_usd=perp_fees,
        fixed_costs_usd=fixed, net_usd=net, smallest_clip_usd=smallest_clip,
        min_order_usd=venue.perp_min_order_usd,
        economic_minimum_usd=economic_min, mechanical_minimum_usd=mechanical_min,
        reserve_minimum_usd=reserve_min, checks=checks,
    )


SHORT_BLOCKER = {
    "net_carry_positive": "net<0",
    "overhead_share": "gas",
    "clip_is_tradeable": "clip",
    "collateral_clears_minimum": "min",
    "reserve_is_meaningful": "rsv",
}


def dca_drag(venue, contribution_usd: float, *, per_year: int = 12,
             gas_per_tx_usd: float = 0.50) -> dict[str, float]:
    """What it costs to add money to the core on a given cadence.

    Every contribution is a swap, so it pays 0.875% plus gas. The percentage
    part is cadence-blind -- it costs the same to buy $1,200 once or twelve
    times -- but the gas is per transaction, so a small monthly buy pays it
    twelve times on a twelfth of the money each time.

    The fee is not a reason to skip contributing. It is a reason to batch.
    """
    if contribution_usd <= 0.0 or per_year <= 0:
        raise ValueError("contribution and cadence must be positive")
    swap = contribution_usd * (venue.swap_fee_pct + venue.swap_slippage_pct)
    per_contribution = swap + gas_per_tx_usd
    contributed = contribution_usd * per_year
    total = per_contribution * per_year
    return {
        "per_contribution_usd": per_contribution,
        "per_contribution_pct": per_contribution / contribution_usd,
        "annual_contributed_usd": contributed,
        "annual_cost_usd": total,
        "annual_drag_pct": total / contributed,
        "gas_share": (gas_per_tx_usd * per_year) / total if total else 0.0,
    }


def dca_table(venue, annual_usd: float = 1_200.0,
              cadences=((12, "monthly"), (6, "every 2 months"),
                        (4, "quarterly"), (2, "twice a year")),
              gas_per_tx_usd: float = 0.50) -> str:
    """Same money, different cadence. Only the gas changes -- but it does."""
    head = (f"{'cadence':<16} {'per buy':>9} {'cost each':>11} {'per buy %':>10} "
            f"{'year cost':>10} {'year drag':>10} {'gas share':>10}")
    lines = [head, "-" * len(head)]
    for per_year, label in cadences:
        amount = annual_usd / per_year
        d = dca_drag(venue, amount, per_year=per_year, gas_per_tx_usd=gas_per_tx_usd)
        lines.append(
            f"{label:<16} {amount:>9,.0f} {d['per_contribution_usd']:>11,.2f} "
            f"{d['per_contribution_pct']:>10.2%} {d['annual_cost_usd']:>10,.2f} "
            f"{d['annual_drag_pct']:>10.2%} {d['gas_share']:>10.0%}")
    lines.append("")
    lines.append(f"${annual_usd:,.0f} a year at {venue.swap_fee_pct:.3%} a swap plus "
                 f"${gas_per_tx_usd:.2f} gas. The swap fee is cadence-blind;")
    lines.append("the gas is not, so batching only saves the gas -- real on an "
                 "L1, minor on an L2.")
    return "\n".join(lines)


def ramp_table(cfg: StrategyConfig, *, funding_apr: float = 0.15,
               avg_hedge_ratio: float = 0.50,
               costs: OperatingCosts | None = None,
               capitals=(200, 500, 1_000, 2_500, 5_000, 10_000, 25_000),
               cash_floor_pct: float = 0.10) -> str:
    """What changes as the account grows. The point of a DCA plan is this table."""
    head = (f"{'capital':>9} {'core':>9} {'gross':>9} {'drag':>8} {'fixed':>8} "
            f"{'net/yr':>9} {'net %':>7} {'clip':>8} {'verdict':>11}")
    lines = [head, "-" * len(head)]
    for cap in capitals:
        v = assess_viability(cfg, float(cap), funding_apr=funding_apr,
                             avg_hedge_ratio=avg_hedge_ratio, costs=costs,
                             cash_floor_pct=cash_floor_pct)
        verdict = "viable" if v.viable else "/".join(
            SHORT_BLOCKER.get(b, b) for b in blockers(v.checks))[:11]
        lines.append(
            f"{cap:>9,} {v.core_usd:>9,.0f} {v.gross_carry_usd:>9,.2f} "
            f"{-v.collateral_drag_usd:>8,.2f} {-v.fixed_costs_usd:>8,.2f} "
            f"{v.net_usd:>9,.2f} {v.net_pct:>+7.2%} {v.smallest_clip_usd:>8,.2f} "
            f"{verdict:>11}")
    return "\n".join(lines)
