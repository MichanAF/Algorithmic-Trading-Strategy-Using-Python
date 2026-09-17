"""How long you have to hold a hedge before it has paid for itself.

This module answers the "how long do I hold?" question, and it answers it with
arithmetic rather than a view, because on MetaMask the arithmetic is lopsided
enough to decide the question on its own.

Put a hedge on spot **you already own** and the only new cost is the perp round
trip -- 0.07% taker, or *minus* 0.02% if both legs fill as maker.  At baseline
funding that breaks even in about three days.

Build the same delta-neutral position **from cash**, buying the spot leg for the
purpose, and you have added a 1.95% round trip through MetaMask Swaps.  At
baseline funding, against the 4% the same dollars would have earned sitting in
mUSD, that takes about four months to break even.

    Same position.  Same funding.  3 days versus 120.

That is the whole architecture in one comparison.  The core is bought once and
held; the hedge is the only thing that moves.  ``from_cash=True`` exists so you
can watch the alternative fail rather than take my word for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .venue import MetaMaskVenue

INF = float("inf")


@dataclass(frozen=True)
class CarryQuote:
    """The economics of one hedge, at one funding rate, on one venue."""

    funding_apr: float
    funding_per_day: float
    cost_frac: float                 # round-trip cost as a fraction of notional
    capital_multiple: float          # capital tied up per dollar of notional
    opportunity_per_day: float       # mUSD yield forgone, per dollar of notional
    break_even_days: float
    min_hold_days: float
    maker: bool
    from_cash: bool

    @property
    def excess_per_day(self) -> float:
        """Funding earned per day beyond what the same capital earns in mUSD."""
        return self.funding_per_day - self.opportunity_per_day

    @property
    def excess_apr(self) -> float:
        return self.excess_per_day * 365.0

    def pnl_frac(self, days: float) -> float:
        """Net return per dollar of hedge notional after holding ``days``."""
        return self.funding_per_day * days - self.cost_frac

    def net_apr(self, days: float) -> float:
        """Annualised return on the capital the hedge actually ties up."""
        if days <= 0.0 or self.capital_multiple <= 0.0:
            return 0.0
        return (self.pnl_frac(days) / days) * 365.0 / self.capital_multiple

    def worth_it(self, days: float) -> bool:
        return days >= self.min_hold_days and self.excess_per_day > 0.0

    def report(self) -> str:
        basis = "from cash (buys the spot leg)" if self.from_cash else "on spot already owned"
        fills = "maker both legs" if self.maker else "taker both legs"
        be = ("never" if self.break_even_days == INF
              else "immediate" if self.break_even_days <= 0.0
              else f"{self.break_even_days:.1f} days")
        lines = [
            f"carry quote -- {basis}, {fills}",
            f"  funding            {self.funding_apr:>8.2%} APR "
            f"({self.funding_per_day:.4%}/day on notional)",
            f"  round-trip cost    {self.cost_frac:>8.3%} of notional",
            f"  capital tied up    {self.capital_multiple:>8.2f}x notional",
            f"  mUSD forgone       {self.opportunity_per_day * 365.0:>8.2%} APR "
            f"on that capital",
            f"  excess over mUSD   {self.excess_apr:>8.2%} APR",
            f"  break-even hold    {be:>8}",
            f"  minimum hold       {self.min_hold_days:>8.1f} days",
        ]
        for d in (1, 3, 7, 30, 90):
            verdict = "ok " if self.pnl_frac(d) > 0 else "LOSS"
            lines.append(f"    {d:>3}d: {self.pnl_frac(d):+.3%} of notional "
                         f"({self.net_apr(d):+7.2%} APR on capital)  {verdict}")
        return "\n".join(lines)


def quote_carry(venue: MetaMaskVenue, funding_apr: float, *, leverage: float,
                from_cash: bool = False, maker: bool = True,
                notional_usd: float = 0.0, withdraw: bool = False,
                musd_apy: float | None = None,
                safety_multiple: float = 3.0,
                floor_days: float = 0.0) -> CarryQuote:
    """Price one hedge.

    ``from_cash`` is the difference between an overlay and a trade.  False means
    the spot leg is already yours and the hedge's only cost is the perp round
    trip.  True adds both MetaMask Swaps legs, and is how you discover that
    building delta-neutral from scratch on this venue is a four-month
    commitment at baseline funding.

    ``safety_multiple`` is why ``min_hold_days`` is not ``break_even_days``.
    Breaking even is not a reason to do something; it is the point at which you
    have worked for free.  Three times break-even is the default bar.
    """
    if leverage <= 0.0:
        raise ValueError("leverage must be positive")
    yield_apy = venue.musd_apy if musd_apy is None else musd_apy

    cost = venue.perp_cost_pct(maker=maker, round_trip=True)
    if from_cash:
        cost += venue.spot_cost_pct(round_trip=True)
    if withdraw and notional_usd > 0.0:
        cost += venue.perp_withdrawal_fee_usd / notional_usd

    # Capital per dollar of hedge notional.  An overlay only ties up margin; a
    # from-cash trade ties up the spot leg too.
    capital_multiple = (1.0 + 1.0 / leverage) if from_cash else (1.0 / leverage)

    funding_per_day = funding_apr / 365.0
    opportunity_per_day = capital_multiple * (yield_apy / 365.0)
    excess = funding_per_day - opportunity_per_day

    # Break-even is "when does this beat leaving the money in mUSD".  A maker
    # round trip has a *negative* cost -- the rebate pays you to open -- so the
    # answer is "immediately", not a negative number of days.
    if excess <= 0.0:
        break_even = INF
    elif cost <= 0.0:
        break_even = 0.0
    else:
        break_even = cost / excess
    min_hold = max(floor_days, break_even * safety_multiple) if break_even != INF else INF

    return CarryQuote(
        funding_apr=funding_apr, funding_per_day=funding_per_day,
        cost_frac=cost, capital_multiple=capital_multiple,
        opportunity_per_day=opportunity_per_day, break_even_days=break_even,
        min_hold_days=min_hold, maker=maker, from_cash=from_cash,
    )


def funding_apr_needed(venue: MetaMaskVenue, hold_days: float, *, leverage: float,
                       from_cash: bool = False, maker: bool = True,
                       musd_apy: float | None = None) -> float:
    """The funding APR that makes a hold of ``hold_days`` break even.

    The inverse of :func:`quote_carry`, and the more useful direction in
    practice: you rarely get to pick funding, but you do get to pick whether a
    given funding regime is worth acting on at the horizon you can commit to.
    """
    if hold_days <= 0.0:
        raise ValueError("hold_days must be positive")
    yield_apy = venue.musd_apy if musd_apy is None else musd_apy
    cost = venue.perp_cost_pct(maker=maker, round_trip=True)
    if from_cash:
        cost += venue.spot_cost_pct(round_trip=True)
    capital_multiple = (1.0 + 1.0 / leverage) if from_cash else (1.0 / leverage)
    opportunity_per_day = capital_multiple * (yield_apy / 365.0)
    return (cost / hold_days + opportunity_per_day) * 365.0


def realised_funding(rates_per_hour, notional: float) -> float:
    """Funding collected by a short over a run of hourly rates.

    Hyperliquid pays hourly on ``position_size * oracle_price * rate``, so this
    is a plain sum and not a compounding one -- the payment leaves the margin
    account rather than being reinvested into the position.
    """
    return float(sum(rates_per_hour)) * notional


def break_even_table(venue: MetaMaskVenue, *, leverage: float,
                     aprs=(0.05, 0.11, 0.15, 0.25, 0.45, 0.75),
                     musd_apy: float | None = None) -> str:
    """The table that settles the hold-duration question."""
    head = (f"{'funding APR':>12} | {'overlay, maker':>15} | {'overlay, taker':>15} "
            f"| {'from cash, taker':>17}")
    lines = [head, "-" * len(head)]
    for apr in aprs:
        cells = []
        for from_cash, maker in ((False, True), (False, False), (True, False)):
            q = quote_carry(venue, apr, leverage=leverage, from_cash=from_cash,
                            maker=maker, musd_apy=musd_apy)
            cells.append("never" if q.break_even_days == INF
                         else "immediate" if q.break_even_days <= 0.0
                         else f"{q.break_even_days:.1f}d")
        lines.append(f"{apr:>11.1%}  | {cells[0]:>15} | {cells[1]:>15} | {cells[2]:>17}")
    lines.append("")
    lines.append(f"break-even hold at {leverage:g}x. 'never' means funding does not "
                 f"clear the {venue.musd_apy:.0%} mUSD yield on the capital tied up,")
    lines.append("so the position loses to leaving the money in the Money Account.")
    return "\n".join(lines)
