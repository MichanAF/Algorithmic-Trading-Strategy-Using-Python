"""The split -- derived, not chosen.

The usual way to answer "how should I divide my money?" is a pie chart someone
liked the look of.  This module refuses to do that, because on an isolated-margin
venue the split is not a preference.  It falls out of two numbers you *do* get
to choose:

1. **How much of the core you want hedged at maximum** (``max_hedge_ratio``).
2. **The rally the hedge has to survive with nobody watching**
   (``survive_rally_pct``).

Everything else is arithmetic.  Surviving an adverse move ``r`` on a perp with
maintenance margin ``m`` takes collateral of ``r(1+m) + m`` per dollar of hedge
notional -- 51.5% to survive +50% on BTC, 35.4% to survive +35%.  Multiply by
how much you intend to hedge, and the perp sleeve has claimed its share before
the spot sleeve gets a vote.

Why this matters more here than on a normal venue: **MetaMask Perps is isolated
margin only.**  Your spot BTC is not collateral for your short BTC perp.  In a
squeeze the short is liquidated on its own merits while the spot leg -- up the
exact amount that killed it -- sits in the wallet unable to help.  You end up
having realised the entire hedge loss and holding an unhedged book at the top.

Undersizing the perp sleeve is therefore not a mild inefficiency.  It is the
one mistake on this venue that converts a hedged position into a leveraged
short that dies at the worst possible moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import StrategyConfig
from .venue import MetaMaskVenue


def margin_fraction_for_survival(adverse_move: float, maintenance: float) -> float:
    """Collateral per dollar of hedge notional to survive ``adverse_move``."""
    if adverse_move < 0.0:
        raise ValueError("adverse_move must be non-negative")
    return adverse_move * (1.0 + maintenance) + maintenance


def liquidation_move(leverage: float, maintenance: float) -> float:
    """Adverse move that liquidates a short at ``leverage``."""
    if leverage <= 0.0:
        raise ValueError("leverage must be positive")
    return (1.0 / leverage - maintenance) / (1.0 + maintenance)


def leverage_for_survival(adverse_move: float, maintenance: float) -> float:
    return 1.0 / margin_fraction_for_survival(adverse_move, maintenance)


@dataclass(frozen=True)
class AssetSlice:
    symbol: str
    hedge_symbol: str
    weight: float
    tier: str
    maintenance: float
    core_usd: float
    staked_usd: float
    max_hedge_notional: float
    margin_fraction: float
    collateral_usd: float          # total earmarked: posted + staged reserve
    posted_usd: float              # what is actually on HyperCore at full hedge
    reserve_usd: float             # staged on Arbitrum, one transaction away
    liquidation_at: float          # adverse move that kills the posted-only short

    @property
    def liquid_core_usd(self) -> float:
        return self.core_usd - self.staked_usd


@dataclass
class Allocation:
    """A complete book: what sits where, and what it is for."""

    capital_usd: float
    core_usd: float
    collateral_usd: float
    posted_usd: float
    reserve_usd: float
    cash_usd: float
    staked_usd: float
    max_hedge_ratio: float
    leverage: float
    survive_rally_pct: float
    blended_margin_fraction: float
    slices: tuple[AssetSlice, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Staged where it can actually be moved in a hurry, which costs it the mUSD
    # yield.  Tracked here because `expected_annual_yield` must not credit
    # interest the reserve is deliberately not earning.
    reserve_on_arbitrum: bool = True

    # -- exposure ------------------------------------------------------- #

    def net_delta_usd(self, hedge_ratio: float) -> float:
        """Dollars of price exposure left after hedging ``hedge_ratio`` of the core."""
        return self.core_usd * (1.0 - hedge_ratio)

    def hedge_notional(self, hedge_ratio: float) -> float:
        return self.core_usd * hedge_ratio

    def margin_required(self, hedge_ratio: float) -> float:
        return self.hedge_notional(hedge_ratio) / self.leverage

    def liquid_core_usd(self) -> float:
        return self.core_usd - self.staked_usd

    def core_liquid_pct(self) -> float:
        return self.liquid_core_usd() / self.core_usd if self.core_usd else 0.0

    # -- yield ---------------------------------------------------------- #

    def expected_annual_yield(self, venue: MetaMaskVenue, funding_apr: float,
                              hedge_ratio: float, staking_gross_apr: float = 0.03
                              ) -> dict[str, float]:
        """Yield decomposition, in dollars a year, before price moves.

        Deliberately excludes price return: the point of the hedged sleeve is
        that its P&L does not depend on direction, and mixing a carry estimate
        with a price forecast is how carry strategies end up reported as alpha.
        """
        funding = self.hedge_notional(hedge_ratio) * funding_apr
        cash = self.cash_usd * venue.musd_apy
        reserve = self.reserve_usd * venue.musd_apy if not self.reserve_on_arbitrum else 0.0
        staking = self.staked_usd * venue.net_staking_apr(staking_gross_apr)
        total = funding + cash + reserve + staking
        return {
            "funding": funding, "musd_cash": cash, "musd_reserve": reserve,
            "eth_staking": staking, "total": total,
            "on_capital": total / self.capital_usd if self.capital_usd else 0.0,
        }

    # -- presentation ---------------------------------------------------- #

    def report(self) -> str:
        cap = self.capital_usd
        def pct(x: float) -> str:
            return f"{x / cap:6.1%}" if cap else "   n/a"

        lines = [
            f"allocation on ${cap:,.0f}",
            f"  derived from: hedge up to {self.max_hedge_ratio:.0%} of core, "
            f"survive a {self.survive_rally_pct:+.0%} rally unattended, "
            f"{self.leverage:g}x on the short",
            "",
            f"  {'bucket':<28} {'USD':>12}  {'share':>7}  instrument",
            f"  {'-' * 28} {'-' * 12}  {'-' * 7}  {'-' * 34}",
            f"  {'core spot (held)':<28} {self.core_usd:>12,.0f}  {pct(self.core_usd)}  "
            f"native BTC / ETH / SOL in wallet",
            f"  {'  of which staked':<28} {self.staked_usd:>12,.0f}  {pct(self.staked_usd)}  "
            f"MetaMask pooled staking (illiquid)",
            f"  {'perp collateral (posted)':<28} {self.posted_usd:>12,.0f}  {pct(self.posted_usd)}  "
            f"USDC on HyperCore",
            f"  {'margin reserve (staged)':<28} {self.reserve_usd:>12,.0f}  {pct(self.reserve_usd)}  "
            f"USDC on Arbitrum, 1 tx from the perp",
            f"  {'idle cash':<28} {self.cash_usd:>12,.0f}  {pct(self.cash_usd)}  "
            f"mUSD / Money Account",
            f"  {'-' * 28} {'-' * 12}  {'-' * 7}",
            f"  {'total':<28} "
            f"{self.core_usd + self.posted_usd + self.reserve_usd + self.cash_usd:>12,.0f}  "
            f"{pct(self.core_usd + self.posted_usd + self.reserve_usd + self.cash_usd)}",
            "",
            f"  core detail ({self.blended_margin_fraction:.1%} collateral per $ hedged, blended)",
            f"  {'asset':<8} {'wt':>6} {'core $':>11} {'hedge $':>11} "
            f"{'collat $':>10} {'tier':>10} {'liq at':>8}",
        ]
        for s in self.slices:
            lines.append(
                f"  {s.symbol:<8} {s.weight:>6.1%} {s.core_usd:>11,.0f} "
                f"{s.max_hedge_notional:>11,.0f} {s.collateral_usd:>10,.0f} "
                f"{s.tier:>10} {s.liquidation_at:>+8.1%}")
        if self.notes:
            lines.append("")
            lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def allocate(cfg: StrategyConfig, capital_usd: float | None = None,
             cash_floor_pct: float = 0.10) -> Allocation:
    """Turn a config into a book.

    Solves ``core * (1 + h_max * margin_fraction) = deployable`` for the core,
    which is the only sizing equation in the strategy.  Everything the perp
    sleeve needs is claimed first; the spot sleeve gets what is left, not the
    other way round.
    """
    capital = float(cfg.capital_usd if capital_usd is None else capital_usd)
    if capital <= 0.0:
        raise ValueError("capital_usd must be positive")
    if not 0.0 <= cash_floor_pct < 1.0:
        raise ValueError("cash_floor_pct must be in [0, 1)")

    venue, h_max = cfg.venue, cfg.hedge.max_hedge_ratio
    survive, lev = cfg.risk.survive_rally_pct, cfg.hedge.leverage
    posted_frac = 1.0 / lev

    # Blended collateral requirement, weighted by the core.  Per asset, because
    # a SOL hedge needs 2.5% maintenance where a BTC hedge needs 1%.
    per_asset_margin = {}
    for symbol, weight in cfg.core.weights.items():
        hedge_sym = cfg.hedge_symbol(symbol)
        # The venue decides this, not the strategy: under cross margin the spot
        # leg is already collateral and the requirement collapses toward zero;
        # under isolated margin you fund the whole survivable move yourself.
        per_asset_margin[symbol] = venue.hedge_collateral_fraction(
            hedge_sym, survive, lev)
    blended = sum(cfg.core.weights[s] * m for s, m in per_asset_margin.items())

    deployable = capital * (1.0 - cash_floor_pct)
    core = deployable / (1.0 + h_max * blended)

    slices: list[AssetSlice] = []
    posted_total = reserve_total = collateral_total = staked_total = 0.0
    for symbol, weight in cfg.core.weights.items():
        hedge_sym = cfg.hedge_symbol(symbol)
        tier = venue.tier_for(hedge_sym)
        core_i = core * weight
        staked_i = core_i * cfg.core.staked_pct.get(symbol, 0.0)
        notional_i = core_i * h_max
        margin_frac_i = per_asset_margin[symbol]
        collateral_i = notional_i * margin_frac_i
        # Never post more than the requirement: on a cross-margin venue the
        # requirement can be zero, and posting 1/leverage anyway would idle
        # capital the venue never asked for.
        posted_i = notional_i * min(posted_frac, margin_frac_i)
        reserve_i = max(0.0, collateral_i - posted_i)
        slices.append(AssetSlice(
            symbol=symbol, hedge_symbol=hedge_sym, weight=weight, tier=tier.name,
            maintenance=tier.maintenance_margin, core_usd=core_i, staked_usd=staked_i,
            max_hedge_notional=notional_i, margin_fraction=margin_frac_i,
            collateral_usd=collateral_i, posted_usd=posted_i, reserve_usd=reserve_i,
            liquidation_at=liquidation_move(lev, tier.maintenance_margin),
        ))
        posted_total += posted_i
        reserve_total += reserve_i
        collateral_total += collateral_i
        staked_total += staked_i

    cash = capital - core - posted_total - reserve_total

    notes: list[str] = []
    if posted_frac > blended:
        notes.append(
            f"{lev:g}x posts more collateral ({posted_frac:.1%}) than surviving "
            f"{survive:+.0%} requires ({blended:.1%}); the reserve is zero and the "
            f"hedge is over-collateralised, which is safe but idle")
    else:
        notes.append(
            f"{lev:g}x alone liquidates at "
            f"{liquidation_move(lev, cfg.blended_maintenance_margin()):+.1%}; the "
            f"reserve is what extends that to {survive:+.0%} and it only works if "
            f"it is actually staged, not merely intended")
    if cfg.risk.reserve_on_arbitrum:
        notes.append(
            "reserve sits as USDC on Arbitrum, earning nothing, because a "
            "cross-chain hop from mUSD during a squeeze is the one trade that "
            "will not fill in time")
    if staked_total > 0.0:
        notes.append(
            f"${staked_total:,.0f} staked carries full price delta (the perp "
            f"hedges it) but is not liquid and is never perp collateral")
    biggest = max(cfg.core.weights.items(), key=lambda kv: kv[1])
    if biggest[1] > cfg.risk.max_single_asset_weight:
        notes.append(
            f"{biggest[0]} is {biggest[1]:.0%} of core, above the "
            f"{cfg.risk.max_single_asset_weight:.0%} concentration limit")
    if capital * (1.0 - cash_floor_pct) < venue.perp_min_funding_usd * 20:
        notes.append(
            f"at this size the ${venue.perp_withdrawal_fee_usd:.0f} withdrawal fee "
            f"and ${venue.perp_min_funding_usd:.0f} minimum are material; leave "
            f"collateral parked on HyperCore between hedges")

    alloc = Allocation(
        capital_usd=capital, core_usd=core, collateral_usd=collateral_total,
        posted_usd=posted_total, reserve_usd=reserve_total, cash_usd=cash,
        staked_usd=staked_total, max_hedge_ratio=h_max, leverage=lev,
        survive_rally_pct=survive, blended_margin_fraction=blended,
        slices=tuple(slices), notes=tuple(notes),
    )
    alloc.reserve_on_arbitrum = cfg.risk.reserve_on_arbitrum
    return alloc
