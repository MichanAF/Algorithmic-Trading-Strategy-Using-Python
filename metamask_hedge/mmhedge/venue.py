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
    # How hard a cross-margin venue discounts this asset when it is posted as
    # collateral.  Majors are haircut lightly; illiquid tokens are haircut
    # hard, and plenty of venues will not take them as collateral at all --
    # which is a haircut of 1.0 and puts the position back in the isolated
    # case however 'cross' the account claims to be.  This number, not the
    # maintenance margin, is what decides whether a hedged meme pair survives.
    collateral_haircut: float = 0.05

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
MAJOR = MarginTier("major", 50.0, 0.01, ("BTC", "ETH"), collateral_haircut=0.05)
LARGE = MarginTier("large", 20.0, 0.025, ("SOL", "HYPE", "XRP", "LINK"),
                   collateral_haircut=0.10)
LONG_TAIL = MarginTier("long_tail", 10.0, 0.05, ("most alts",),
                       collateral_haircut=0.30)
# Meme perps exist and are liquid on the majors, but the tier that matters is
# not the margin bracket -- it is that the underlying routinely moves further
# in a day than any sane short can survive.  See `squeeze_survival` below.
MEME = MarginTier("meme", 10.0, 0.05, ("DOGE", "SHIB", "PEPE", "BONK", "WIF"),
                  verified=False, collateral_haircut=0.40)
NON_CRYPTO = MarginTier("non_crypto", 10.0, 0.05,
                        ("NVDA", "TSLA", "COIN", "gold", "FX"), verified=False,
                        collateral_haircut=0.20)

TIERS: dict[str, MarginTier] = {
    t.name: t for t in (MAJOR, LARGE, LONG_TAIL, MEME, NON_CRYPTO)}

# Which tier a symbol lands in.  Anything unlisted falls to LONG_TAIL, which is
# the safe direction to be wrong in.
SYMBOL_TIERS: dict[str, str] = {
    "BTC": "major", "WBTC": "major", "CBBTC": "major",
    "ETH": "major", "WETH": "major", "STETH": "major", "WSTETH": "major",
    "SOL": "large", "HYPE": "large", "XRP": "large",
    "LINK": "large", "AVAX": "large", "BNB": "large",
    # DOGE is large-cap and liquid, but it trades like a meme coin and a real
    # venue haircuts it accordingly. The conservative tier is the right one.
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "BONK": "meme",
    "WIF": "meme", "FLOKI": "meme", "PENGU": "meme", "SPX": "meme",
}


@dataclass(frozen=True)
class Venue:
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
    # Smallest perp order worth attempting.  MetaMask documents a $10 minimum
    # to *fund* the account; Hyperliquid also enforces a per-market minimum
    # order size, which varies.  $10 is a conservative stand-in -- check the
    # market you actually trade before relying on a small clip filling.
    perp_min_order_usd: float = 10.0
    perp_withdrawal_asset: str = "USDC"
    perp_withdrawal_chain: str = "Arbitrum"
    isolated_margin_only: bool = True
    # Cross margin with spot as collateral is the single biggest structural
    # difference between venues, and it is worth more than any fee.  When the
    # spot leg backs the short, a rally that hurts the short is paid for by the
    # spot gain *inside the same margin account*, and the delta-neutral pair
    # stops being liquidatable by ordinary moves.
    cross_margin: bool = False
    # Discount the venue applies to crypto posted as collateral.  0.05 means
    # BTC counts for 95 cents on the dollar.  Illiquid tokens are haircut far
    # harder, and many venues will not accept them as collateral at all --
    # which silently puts you back in the isolated case.
    collateral_haircut: float = 0.05

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

    def hedge_collateral_fraction(self, symbol: str, survive_rally: float,
                                  leverage: float = 2.0) -> float:
        """Collateral to set aside per dollar of hedge notional.

        Under **isolated** margin the spot leg is irrelevant and you must fund
        the whole survivable move yourself: ``r(1+m) + m``, which is 51.5% to
        reach +50% on BTC.

        Under **cross** margin the spot leg is already posted, and it gains
        exactly what the short loses. Requiring equity at the target move::

            (1-h)N(1+r) - N*r + X  >=  m*N*(1+r)
            X/N  >=  m(1+r) - (1-h)(1+r) + r

        For a major that is negative at any sane target -- the haircut spot
        covers the perp's margin many times over -- so the answer is zero and
        essentially all of the capital can sit in the core. For a meme coin at
        a +200% target it is about 35%, because the haircut is 40% and the
        collateral stops keeping up.

        This is the single largest capital-efficiency difference between the
        two venues, and it is much bigger than any fee on this page.
        """
        tier = self.tier_for(symbol)
        m = tier.maintenance_margin
        if not self.cross_margin:
            return tier.margin_fraction_for_survival(survive_rally)
        h = max(tier.collateral_haircut, self.collateral_haircut)
        if h >= 1.0:                      # not accepted as collateral at all
            return tier.margin_fraction_for_survival(survive_rally)
        r = survive_rally
        return max(0.0, m * (1.0 + r) - (1.0 - h) * (1.0 + r) + r)

    def hedged_liquidation_move(self, symbol: str, leverage: float) -> float:
        """Where a *delta-neutral pair* dies, which is a different question.

        Under isolated margin the spot leg is irrelevant, so this is just the
        short's own liquidation point.

        Under cross margin with the spot posted as collateral, equity is
        ``(1-h)*N*(1+r) - N*r`` against a requirement of ``m*N*(1+r)``, so::

            r = (1 - h - m) / (h + m)

        With a 5% haircut on BTC that is about +1567%: the pair is effectively
        unliquidatable by price. With a 40% haircut on an illiquid token it is
        about +122% -- which a meme coin can and does do.

        The catch that is easy to miss: if the venue will not accept that token
        as collateral at all, the haircut is 100% and you are back to the
        isolated case however 'cross' the account claims to be.
        """
        tier = self.tier_for(symbol)
        m = tier.maintenance_margin
        if not self.cross_margin:
            return self.liquidation_move(symbol, leverage)
        # The tier's own haircut wins: posting PEPE as collateral is nothing
        # like posting BTC, and a venue-wide number hides exactly that.
        h = max(tier.collateral_haircut, self.collateral_haircut)
        if h >= 1.0:
            return self.liquidation_move(symbol, leverage)
        denom = h + m
        if denom <= 0.0:
            return float("inf")
        return (1.0 - h - m) / denom

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

    def with_overrides(self, **kwargs) -> "Venue":
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
                f"collateral haircut {t.collateral_haircut:.0%}, "
                f"3x short survives {t.liquidation_move(3.0):+.1%}{flag}")
        return "\n".join(lines)


MetaMaskVenue = Venue          # back-compat: the dataclass is venue-shaped


METAMASK = Venue()

# OKX, VIP0 retail tier, September 2026.  Two things flip versus MetaMask: spot
# is nearly nine times cheaper, and the unified account posts spot as collateral
# for the perp.  One thing flips the other way: the perp itself costs more, and
# there is no maker rebate.
OKX = Venue(
    name="okx",
    swap_fee_pct=0.0010,           # 0.10% spot taker (0.08% maker)
    swap_slippage_pct=0.0005,      # deep books on majors
    spot_gas_usd=0.0,              # internal trades: no chain, no gas
    perp_taker_pct=0.0005,         # 0.05%
    perp_maker_pct=0.0002,         # 0.02% -- a cost, not a rebate
    perp_withdrawal_fee_usd=0.0,   # moving between internal accounts is free
    perp_min_funding_usd=0.0,
    perp_min_order_usd=5.0,
    isolated_margin_only=False,
    cross_margin=True,
    collateral_haircut=0.05,
    funding_interval_hours=8.0,    # vs Hyperliquid's hourly stamp
    # Simple Earn flexible USDT, order of magnitude. Variable and unverified;
    # left non-zero deliberately, because scoring OKX with no cash yield would
    # hand it a free win on every opportunity-cost comparison in this package.
    musd_apy=0.03,
)

VENUES: dict[str, Venue] = {"metamask": METAMASK, "okx": OKX}


def compare_venues(a: Venue, b: Venue, symbol: str = "BTC",
                   leverage: float = 2.0) -> str:
    """The side-by-side that decides where to run the book."""
    rows = [
        ("spot round trip", lambda v: f"{v.spot_cost_pct(round_trip=True):.3%}"),
        ("perp round trip (taker)",
         lambda v: f"{v.perp_cost_pct(round_trip=True):.3%}"),
        ("perp round trip (maker)",
         lambda v: f"{v.perp_cost_pct(maker=True, round_trip=True):+.3%}"),
        ("funding cadence", lambda v: f"{v.funding_interval_hours:g}h"),
        ("margin model",
         lambda v: "cross, spot as collateral" if v.cross_margin else "isolated only"),
        (f"{leverage:g}x short alone liquidates",
         lambda v: f"{v.liquidation_move(symbol, leverage):+.1%}"),
        ("delta-neutral pair liquidates",
         lambda v: ("never (by price)"
                    if v.hedged_liquidation_move(symbol, leverage) > 5.0
                    else f"{v.hedged_liquidation_move(symbol, leverage):+.0%}")),
        ("idle cash yield", lambda v: f"{v.musd_apy:.2%}"),
        ("custody", lambda v: "self" if v.name == "metamask" else "exchange"),
    ]
    width = max(len(r[0]) for r in rows) + 2
    lines = [f"{symbol} at {leverage:g}x",
             f"{'':<{width}} {a.name:>26} {b.name:>26}",
             "-" * (width + 54)]
    for label, fn in rows:
        lines.append(f"{label:<{width}} {fn(a):>26} {fn(b):>26}")
    return "\n".join(lines)
