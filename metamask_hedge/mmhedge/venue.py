"""What MetaMask actually charges, and what Hyperliquid actually does.

Every number in this module is a venue fact, not a strategy choice, and the
whole strategy is downstream of one asymmetry in them:

    MetaMask Swaps charge **0.875%**, built into the quote.
    Hyperliquid taker fees through MetaMask Perps are **0.035%**.

Moving a dollar of exposure with the spot leg costs twenty-five times what it
costs to move the same dollar with the perp leg.  That single ratio decides the
architecture: the spot book is bought once and left alone, and *all* exposure
management happens in the perp overlay.  A strategy that rebalances by swapping
is paying 1.75% a round trip to do what 0.07% would have done.

The second fact that shapes everything is that **MetaMask Perps is isolated
margin only**.  Your spot BTC is not collateral for your short BTC perp.  They
sit in different places -- spot in the wallet, perp collateral as USDC on
HyperCore -- and a rally that doubles your spot leg will happily liquidate the
short that was supposed to be hedging it.  ``sizing.py`` exists because of this.

Sources, as of September 2026.  Fees change; re-check before sizing anything
real, and override them in the config rather than editing this file:

* MetaMask Swaps fee, staking fee share  -- metamask.io, wallet review coverage
* MetaMask Perps: no MetaMask trading fee, $1 withdrawal (USDC on Arbitrum
  only), $10 minimum funding, isolated margin, up to 50x  -- support.metamask.io
* Hyperliquid taker 0.035% / maker -0.01% rebate, hourly funding, interest
  component 0.01% per 8h, premium clamp +/-0.05%, 4%/hour cap  -- Hyperliquid docs
* mUSD / Money Account yield up to 4% variable APY  -- metamask.io
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

HOURS_PER_YEAR = 24 * 365
HOURS_PER_DAY = 24


@dataclass(frozen=True)
class MarginTier:
    """Max leverage and maintenance margin for one class of perp market.

    ``verified`` is the honest flag.  The crypto tiers are published numbers.
    The equity/commodity tier is a placeholder wide enough not to get anyone
    hurt, and it is marked unverified because it is a guess -- check the
    contract details in the app before you size against it.
    """

    name: str
    max_leverage: float
    maintenance_margin: float
    examples: tuple[str, ...] = ()
    verified: bool = True

    def liquidation_move(self, leverage: float) -> float:
        """Adverse move, as a fraction of entry price, that liquidates.

        For a short at leverage ``L`` with maintenance margin ``m``, equity is
        ``N/L - N*r`` and the requirement is ``m*N*(1+r)``, so liquidation lands
        at::

            r = (1/L - m) / (1 + m)

        A 3x short on BTC survives a +32% rally.  A 10x short survives +8.9%.
        BTC has done +8.9% in an afternoon more than once.
        """
        if leverage <= 0.0:
            raise ValueError("leverage must be positive")
        return (1.0 / leverage - self.maintenance_margin) / (1.0 + self.maintenance_margin)

    def leverage_for_survival(self, adverse_move: float) -> float:
        """The highest leverage that still survives ``adverse_move``.

        The inverse of :meth:`liquidation_move`.  Ask for +50% and it answers
        1.94x, which is the real reason a hedge that must survive unattended is
        barely levered at all.
        """
        if adverse_move < 0.0:
            raise ValueError("adverse_move must be non-negative")
        denom = adverse_move * (1.0 + self.maintenance_margin) + self.maintenance_margin
        return 1.0 / denom

    def margin_fraction_for_survival(self, adverse_move: float) -> float:
        """Collateral per unit of hedge notional needed to survive ``adverse_move``."""
        return adverse_move * (1.0 + self.maintenance_margin) + self.maintenance_margin


# Crypto tiers are Hyperliquid's published brackets.  The exact bracket varies
# per market and with position size; these are the conservative end of each.
MAJOR = MarginTier("major", 50.0, 0.01, ("BTC", "ETH"))
LARGE = MarginTier("large", 20.0, 0.025, ("SOL", "HYPE", "XRP", "DOGE"))
LONG_TAIL = MarginTier("long_tail", 10.0, 0.05, ("most alts",))
NON_CRYPTO = MarginTier("non_crypto", 10.0, 0.05,
                        ("NVDA", "TSLA", "COIN", "gold", "FX"), verified=False)

TIERS: dict[str, MarginTier] = {t.name: t for t in (MAJOR, LARGE, LONG_TAIL, NON_CRYPTO)}

# Which tier a symbol lands in.  Anything unlisted falls to LONG_TAIL, which is
# the safe direction to be wrong in.
SYMBOL_TIERS: dict[str, str] = {
    "BTC": "major", "WBTC": "major", "CBBTC": "major",
    "ETH": "major", "WETH": "major", "STETH": "major", "WSTETH": "major",
    "SOL": "large", "HYPE": "large", "XRP": "large", "DOGE": "large",
    "LINK": "large", "AVAX": "large", "BNB": "large",
}


@dataclass(frozen=True)
class MetaMaskVenue:
    """Costs and rules for the MetaMask instrument set.

    Overridable from config, because every one of these is a number someone
    else controls and can change on a Tuesday.
    """

    name: str = "metamask"

    # -- spot leg: MetaMask Swaps ------------------------------------------ #
    # Built into the quote, so it never shows up as a line item.  It is the
    # single most expensive number here and the reason the core is buy-and-hold.
    swap_fee_pct: float = 0.00875
    swap_slippage_pct: float = 0.0010          # budget, not a fee
    spot_gas_usd: float = 0.50                 # L2 / Solana order of magnitude

    # -- perp leg: Hyperliquid through MetaMask Perps ---------------------- #
    # MetaMask adds no trading fee of its own; these are Hyperliquid's.
    perp_taker_pct: float = 0.00035
    perp_maker_pct: float = -0.00010           # negative: a rebate
    perp_deposit_fee_usd: float = 0.0
    perp_withdrawal_fee_usd: float = 1.00
    perp_min_funding_usd: float = 10.0
    perp_withdrawal_asset: str = "USDC"
    perp_withdrawal_chain: str = "Arbitrum"
    isolated_margin_only: bool = True

    # -- funding ----------------------------------------------------------- #
    # F = premium + clamp(interest - premium, -0.0005, 0.0005), sampled every
    # 5s, paid hourly on oracle-price notional.
    funding_interval_hours: float = 1.0
    funding_interest_per_hour: float = 0.0000125   # 0.01% per 8h, longs pay shorts
    funding_clamp: float = 0.0005
    funding_cap_per_hour: float = 0.04

    # -- cash leg ----------------------------------------------------------- #
    musd_apy: float = 0.04                     # Money Account, variable
    staking_fee_share: float = 0.15            # MetaMask's cut of ETH rewards

    tiers: dict[str, MarginTier] = field(default_factory=lambda: dict(TIERS))
    symbol_tiers: dict[str, str] = field(default_factory=lambda: dict(SYMBOL_TIERS))

    # ------------------------------------------------------------------ #
    # tier lookup
    # ------------------------------------------------------------------ #

    def tier_for(self, symbol: str) -> MarginTier:
        key = self.symbol_tiers.get(symbol.strip().upper(), "long_tail")
        return self.tiers[key]

    def max_leverage(self, symbol: str) -> float:
        return self.tier_for(symbol).max_leverage

    def liquidation_move(self, symbol: str, leverage: float) -> float:
        return self.tier_for(symbol).liquidation_move(leverage)

    # ------------------------------------------------------------------ #
    # cost of moving exposure
    # ------------------------------------------------------------------ #

    def spot_cost_pct(self, *, round_trip: bool = False) -> float:
        """Cost of moving a dollar of exposure through the spot book."""
        one_way = self.swap_fee_pct + self.swap_slippage_pct
        return one_way * (2.0 if round_trip else 1.0)

    def perp_cost_pct(self, *, maker: bool = False, round_trip: bool = False) -> float:
        """Cost of moving a dollar of exposure through the perp book.

        Maker versus taker is not a rounding detail on a short hold.  Taker both
        ways costs 0.07%; maker both ways *pays* 0.02%.  At baseline funding
        (0.03%/day) that 0.09% gap is three days of carry -- the entire trade.
        """
        one_way = self.perp_maker_pct if maker else self.perp_taker_pct
        return one_way * (2.0 if round_trip else 1.0)

    def rebalance_cost_ratio(self) -> float:
        """How many times more expensive the spot book is than the perp book."""
        perp = abs(self.perp_taker_pct)
        return self.spot_cost_pct() / perp if perp else float("inf")

    def open_hedge_cost_pct(self, *, maker: bool = False) -> float:
        """Cost to put a hedge on, as a fraction of hedge notional."""
        return self.perp_cost_pct(maker=maker)

    def close_hedge_cost_pct(self, *, maker: bool = False,
                             withdraw_notional: float = 0.0) -> float:
        """Cost to take a hedge off, including a withdrawal if you take the cash out.

        The $1 is flat, so it matters at $500 of notional (0.2%) and vanishes at
        $50,000 (0.002%).  It is the reason a small account should leave
        collateral parked on HyperCore between hedges instead of round-tripping
        it to Arbitrum.
        """
        cost = self.perp_cost_pct(maker=maker)
        if withdraw_notional > 0.0:
            cost += self.perp_withdrawal_fee_usd / withdraw_notional
        return cost

    # ------------------------------------------------------------------ #
    # funding
    # ------------------------------------------------------------------ #

    def funding_rate(self, premium: float) -> float:
        """Hyperliquid's hourly funding from a premium index reading.

        ``F = premium + clamp(interest - premium, -0.0005, 0.0005)``, then
        capped at the 4%/hour ceiling.  Positive means longs pay shorts, which
        is the direction this strategy wants to be on.
        """
        i = self.funding_interest_per_hour
        adj = max(-self.funding_clamp, min(self.funding_clamp, i - premium))
        rate = premium + adj
        cap = self.funding_cap_per_hour
        return max(-cap, min(cap, rate))

    def baseline_funding_apr(self) -> float:
        """What funding pays when nothing is going on: about 11% a year."""
        return self.funding_interest_per_hour * HOURS_PER_YEAR

    @staticmethod
    def funding_apr(rate_per_hour: float) -> float:
        return rate_per_hour * HOURS_PER_YEAR

    @staticmethod
    def funding_per_day(rate_per_hour: float) -> float:
        return rate_per_hour * HOURS_PER_DAY

    @staticmethod
    def funding_from_apr(apr: float) -> float:
        return apr / HOURS_PER_YEAR

    def musd_per_day(self) -> float:
        return self.musd_apy / 365.0

    def net_staking_apr(self, gross_apr: float) -> float:
        """What ETH staking pays you after MetaMask keeps its 15%."""
        return gross_apr * (1.0 - self.staking_fee_share)

    def with_overrides(self, **kwargs) -> "MetaMaskVenue":
        return replace(self, **kwargs)

    def summary(self) -> str:
        lines = [
            f"venue: {self.name}",
            f"  spot swap        {self.swap_fee_pct:.3%} per side "
            f"({self.spot_cost_pct(round_trip=True):.3%} round trip incl. slippage)",
            f"  perp taker       {self.perp_taker_pct:.3%} per side "
            f"({self.perp_cost_pct(round_trip=True):.3%} round trip)",
            f"  perp maker       {self.perp_maker_pct:.3%} per side "
            f"({self.perp_cost_pct(maker=True, round_trip=True):+.3%} round trip)",
            f"  => the perp book is {self.rebalance_cost_ratio():.0f}x cheaper "
            f"than the spot book for moving exposure",
            f"  withdrawal       ${self.perp_withdrawal_fee_usd:.2f} "
            f"({self.perp_withdrawal_asset} on {self.perp_withdrawal_chain} only)",
            f"  margin mode      {'isolated only' if self.isolated_margin_only else 'cross'}"
            f"  <- spot does NOT collateralise the perp",
            f"  funding          hourly, baseline "
            f"{self.baseline_funding_apr():.2%} APR to shorts, "
            f"capped {self.funding_cap_per_hour:.0%}/hour",
            f"  idle cash        {self.musd_apy:.2%} APY in mUSD / Money Account",
        ]
        for t in self.tiers.values():
            flag = "" if t.verified else "  [UNVERIFIED -- check in app]"
            lines.append(
                f"  tier {t.name:<10} max {t.max_leverage:>4.0f}x, "
                f"maintenance {t.maintenance_margin:.2%}, "
                f"3x short survives {t.liquidation_move(3.0):+.1%}{flag}")
        return "\n".join(lines)


METAMASK = MetaMaskVenue()
