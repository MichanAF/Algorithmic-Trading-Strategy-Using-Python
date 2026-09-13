"""Matching a real venue's rules, for contract-priced 5-minute up/down markets.

The engine in ``signal.py`` answers one question -- which way, and how sure --
and it answers it the same way everywhere.  What changes between venues is the
money math, and on a contract-priced market that math is unforgiving:

    **break-even is the price you pay.**

Buy a side at 0.84 and you need to be right 84% of the time to break even.  So
the only number that decides whether a bet exists is the live quote, and a
strategy whose probability model caps out at 0.64 can never buy anything priced
above about 0.60.  It plays the near-coin-flip or it stands down.

The second thing a real venue adds is a clock that runs *inside* the bet.  These
markets trade continuously across the five-minute window, so their price drifts
from roughly 50/50 at the open toward the realised answer at the close.  Late in
a window, a lopsided quote is not an opportunity -- it is the market telling you
the move already happened while your signal was getting stale.  ``StaleWindow``
is the veto that keeps the strategy from walking into that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import StrategyConfig
from .gates import DOWN, FLAT, UP, Check, direction_name
from .risk import RiskDecision, RiskManager
from .signal import Signal


@dataclass(frozen=True)
class VenueRules:
    """How one venue defines and settles the bet."""

    name: str
    window_seconds: int = 300
    # Settlement: start price is the close of the candle before the window,
    # end price is the close of the last candle inside it.  With candles
    # labelled by open time that makes it exactly one 5-minute bar's return.
    settlement_candle_seconds: int = 300
    price_feed: str = "binance-topofbook-mid"
    quote_style: str = "contract_price"
    tie_rule: str = "split"                 # equal prices pay 0.50 to both sides
    chain: str = ""
    settlement_url: str = ""
    # How far into the window we will still open a position.  The whole edge
    # comes from acting on the bar close that started the window.
    max_entry_seconds: int = 45
    min_seconds_to_expiry: int = 60
    # A quote this far from even, this early, means the market already knows
    # something the signal does not.
    max_entry_skew: float = 0.12
    fee_model: str = "flat_bps"             # "none" | "flat_bps" | "polymarket_taker"
    fee_bps: float = 0.0
    fee_coefficient: float = 0.0            # for polymarket_taker: the 0.07
    gas_cost_quote: float = 0.0             # per bet, in quote currency
    min_order_quote: float = 0.0            # venue minimum notional
    tick: float = 0.0                       # smallest price increment
    tie_resolves_to: int = 0                # UP, DOWN, or 0 for split/void
    collateral: str = ""

    def window_bounds(self, window_start_ts: int) -> tuple[int, int]:
        return window_start_ts, window_start_ts + self.window_seconds

    def fee_per_share(self, price: float) -> float:
        """Fee charged per one-dollar contract, in quote currency.

        Polymarket's taker fee is ``0.07 x p x (1-p)`` per share, which is not a
        detail to flatten into basis points: it is **maximised at p = 0.5**, and
        a coin-flip market is exactly where a five-minute direction strategy
        wants to trade.  At 0.50 it costs 1.75 cents a share, or 3.5% of the
        notional, on every single bet.  Posting as a maker avoids it entirely.
        """
        if self.fee_model == "none":
            return 0.0
        if self.fee_model == "polymarket_taker":
            return self.fee_coefficient * price * (1.0 - price)
        # fee_bps is basis points of the contract's 1.00 face value, which is
        # the natural unit for a binary: 175 bps means 1.75 cents per contract
        # whatever you paid for it.  StrategyConfig.break_even_probability uses
        # the same convention, and they must not drift apart.
        return self.fee_bps / 10_000.0

    def effective_price(self, price: float) -> float:
        """What a share really costs once the venue's fee is added."""
        return price + self.fee_per_share(price)


# The market in the Trust Wallet screenshots: "Bitcoin Up or Down", five-minute
# windows on BNB Smart Chain.  Gas is a placeholder -- measure your own and set it.
#
# ``price_feed`` is the one field here you should not trust as a venue-wide
# constant.  Trust Wallet's own rules text cites the Chainlink BTC/USDT
# top-of-book stream, but a live CRYPTO_UP_DOWN market declares **Pyth BTC/USD**
# in its ``variantData``, and the market is the authority.  Read it per market
# with ``predictfun.PredictMarket.feed``; this value is only the default for a
# backtest, where the choice of feed is immaterial next to the fixture itself.
PREDICT_FUN_BTC_5M = VenueRules(
    name="predict-fun-btc-5m",
    window_seconds=300,
    settlement_candle_seconds=300,
    price_feed="pyth-btcusd (per market: read variantData.priceFeedSymbol)",
    quote_style="contract_price",
    tie_rule="split",
    tie_resolves_to=0,                      # equal prices pay 0.50 to both sides
    chain="BNB Smart Chain",
    collateral="USDT",
    settlement_url="https://www.pyth.network/price-feeds/crypto-btc-usd",
    max_entry_seconds=45,
    min_seconds_to_expiry=60,
    max_entry_skew=0.12,
    fee_model="flat_bps",
    # Confirmed: a live CRYPTO_UP_DOWN market carries feeRateBps 200.  What the
    # 200 bps is charged *on* is still unconfirmed -- here, as everywhere in this
    # repository, it is read as basis points of the contract's 1.00 face value,
    # so 2 cents a contract.  That is the pessimistic reading; if it turns out to
    # be charged on the premium paid it is cheaper.  Read it per market with
    # ``PredictMarket.fee_rate_bps`` and set this from a real fill.
    fee_bps=200.0,
    gas_cost_quote=0.30,                    # BNB Chain gas; measure and set
)

# Trust Wallet's Predictions tab is predict.fun, so the two are one venue.
TRUST_WALLET_BTC_5M = PREDICT_FUN_BTC_5M

POLYMARKET_BTC_5M = VenueRules(
    name="polymarket-btc-5m",
    window_seconds=300,
    settlement_candle_seconds=300,
    price_feed="chainlink-btcusd (data.chain.link/streams/btc-usd)",
    quote_style="contract_price",
    # Resolves Up when the end price is >= the start price, so an exact tie pays
    # the UP side and costs the DOWN side.  Not a 50-50 split.
    tie_rule="favor_up",
    tie_resolves_to=UP,
    chain="Polygon",
    collateral="USDC",
    settlement_url="https://data.chain.link/streams/btc-usd",
    max_entry_seconds=45,
    min_seconds_to_expiry=60,
    max_entry_skew=0.12,
    fee_model="polymarket_taker",
    fee_coefficient=0.07,                   # fee/share = 0.07 x p x (1-p)
    gas_cost_quote=0.0,                     # gas-free CLOB; maker fee is zero
    min_order_quote=5.0,
    tick=0.01,
)

VENUES = {v.name: v for v in (PREDICT_FUN_BTC_5M, POLYMARKET_BTC_5M)}


@dataclass
class MarketQuote:
    """A live market as it is being offered right now.

    ``up_price`` and ``down_price`` are what one contract costs, each settling
    at 1.00 if its side wins.  They normally sum to slightly more than 1: that
    excess is the venue's margin, and it is charged whichever side you take.
    """

    window_start_ts: int
    up_price: float
    down_price: float
    observed_ts: int
    rules: VenueRules = TRUST_WALLET_BTC_5M

    def __post_init__(self) -> None:
        for name in ("up_price", "down_price"):
            value = float(getattr(self, name))
            if not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be strictly between 0 and 1, got {value}")
            setattr(self, name, value)

    @classmethod
    def from_percent(cls, window_start_ts: int, down_percent: float,
                     observed_ts: int, rules: VenueRules = TRUST_WALLET_BTC_5M,
                     overround: float = 0.0) -> "MarketQuote":
        """Build from a single displayed percentage, as the app shows it.

        The screenshot shows one number ("DOWN 84%").  Treat it as the price of
        the DOWN contract and the complement as UP, plus any overround you have
        measured on the pair.
        """
        down = down_percent / 100.0 if down_percent > 1.0 else down_percent
        return cls(window_start_ts=window_start_ts, down_price=down + overround / 2.0,
                   up_price=(1.0 - down) + overround / 2.0,
                   observed_ts=observed_ts, rules=rules)

    @property
    def window_end_ts(self) -> int:
        return self.window_start_ts + self.rules.window_seconds

    @property
    def overround(self) -> float:
        """How much more than 1.00 the two sides cost together: the margin."""
        return self.up_price + self.down_price - 1.0

    @property
    def seconds_into_window(self) -> int:
        return self.observed_ts - self.window_start_ts

    @property
    def seconds_to_expiry(self) -> int:
        return self.window_end_ts - self.observed_ts

    @property
    def skew(self) -> float:
        """Distance from an even market, 0 at 50/50 and 0.5 at a decided one."""
        return abs(self.up_price - self.down_price) / 2.0

    def price_for(self, side: int) -> float:
        if side == UP:
            return self.up_price
        if side == DOWN:
            return self.down_price
        raise ValueError("side must be UP or DOWN")

    def describe(self) -> str:
        start = datetime.fromtimestamp(self.window_start_ts, tz=timezone.utc)
        end = datetime.fromtimestamp(self.window_end_ts, tz=timezone.utc)
        return (f"{start:%H:%M}-{end:%H:%M} UTC  "
                f"UP {self.up_price:.2f} / DOWN {self.down_price:.2f}  "
                f"(overround {self.overround:+.3f}, "
                f"{self.seconds_into_window}s into the window)")


@dataclass
class MarketDecision:
    """What to do about one live market, and why."""

    quote: MarketQuote
    side: int
    p_model: float
    price: float
    edge: float
    expected_value_per_contract: float
    conditions: tuple[Check, ...] = ()
    risk: RiskDecision | None = None
    stake: float = 0.0
    contracts: float = 0.0
    gas_cost: float = 0.0
    approved: bool = False
    blocked_by: tuple[str, ...] = field(default_factory=tuple)
    other_side_edge: float | None = None
    signal_reason: str = ""

    @property
    def answer(self) -> str:
        return "YES" if self.approved else "NO"

    @property
    def expected_profit(self) -> float:
        """Expected profit on this bet in quote currency, after gas."""
        return self.contracts * self.expected_value_per_contract - self.gas_cost

    # Most informative reason first, not the one that happened to fail first.
    # A stale signal and a market that has already moved are the same event seen
    # from two sides, and "the market already decided" is the useful half.
    _REASON_ORDER = ("market_not_decided", "entry_window", "time_to_expiry",
                     "priced_edge", "gas_cost", "gate_signal")

    def brief(self) -> str:
        """One line, for the forty-five seconds you actually have to decide."""
        if self.approved and self.risk is None:
            # Priced without a risk manager: this says the bet is worth taking,
            # not how much to stake. Do not imply a size nobody sized.
            return (f"{direction_name(self.side)} - buy at {self.price:.3f} - "
                    f"edge {self.edge:+.3f} - {self.quote.seconds_to_expiry}s left"
                    f" - no stake sized")
        if self.approved:
            return (f"{direction_name(self.side)} - buy at {self.price:.3f} - "
                    f"stake {self.stake:,.2f} - edge {self.edge:+.3f} - "
                    f"{self.quote.seconds_to_expiry}s left")
        reasons = {
            "gate_signal": self.signal_reason or "no signal",
            "entry_window": f"too late ({self.quote.seconds_into_window}s into the window)",
            "time_to_expiry": f"only {self.quote.seconds_to_expiry}s left",
            "market_not_decided": f"market already decided (skew {self.quote.skew:.2f})",
            "priced_edge": f"price {self.price:.3f} too high for p {self.p_model:.3f}",
            "gas_cost": "stake too small to cover gas",
        }
        if self.side == FLAT:
            # No side was proposed, so nothing downstream has a price to judge.
            return f"NO BET - {self.signal_reason or 'no signal'}"
        blocked = set(self.blocked_by)
        for label in self._REASON_ORDER:
            if label in blocked:
                return f"NO BET - {reasons[label]}"
        first = self.blocked_by[0] if self.blocked_by else "refused"
        return f"NO BET - {reasons.get(first, first)}"

    def report(self) -> str:
        lines = [
            f"market   {self.quote.describe()}",
            f"signal   {direction_name(self.side)}  p_model {self.p_model:.3f}",
            "venue conditions:",
        ]
        lines.extend(f"  {c}" for c in self.conditions)
        if self.risk is not None:
            lines.append(self.risk.report())
        lines.append(
            f"=> {self.answer}"
            + (f"  buy {direction_name(self.side)} at {self.price:.3f}"
               f"  edge {self.edge:+.3f}"
               f"  stake {self.stake:,.2f} ({self.contracts:,.1f} contracts)"
               f"  expected {self.expected_profit:+,.2f} after {self.gas_cost:,.2f} gas"
               if self.approved else ""))
        if self.blocked_by:
            lines.append(f"   blocked by: {', '.join(self.blocked_by)}")
        if self.other_side_edge is not None and self.other_side_edge > 0:
            lines.append(
                f"   note: {direction_name(-self.side)} shows edge "
                f"{self.other_side_edge:+.3f} at "
                f"{self.quote.price_for(-self.side):.3f}. Not taken: it would mean "
                f"betting against our own signal, which is what a stale signal "
                f"looks like from the inside.")
        return "\n".join(lines)


def evaluate_market(signal: Signal, quote: MarketQuote, cfg: StrategyConfig,
                    risk: RiskManager | None = None,
                    rules: VenueRules | None = None,
                    atr_rank: float | None = None) -> MarketDecision:
    """Price one live market against a signal, and size it.

    The gate stack has already decided the side.  This decides whether the price
    on offer makes that side worth buying, and applies the venue's clock.
    """
    rules = rules or quote.rules
    side = signal.side

    if side == FLAT:
        price = float("nan")
        edge = float("nan")
    else:
        price = rules.effective_price(quote.price_for(side))
        edge = signal.p_model - price

    ev_per_contract = (signal.p_model * (1.0 - price) - (1.0 - signal.p_model) * price
                       if side != FLAT else float("nan"))
    other_edge = (None if side == FLAT
                  else (1.0 - signal.p_model)
                  - rules.effective_price(quote.price_for(-side)))

    conditions = [
        Check("gate_signal", side != FLAT and signal.tradable,
              f"stack says {direction_name(side)}"
              + ("" if signal.tradable
                 else f", but blocked by {', '.join(signal.blocked_by)}")),
        Check("entry_window",
              0 <= quote.seconds_into_window <= rules.max_entry_seconds,
              f"{quote.seconds_into_window}s into the window "
              f"<= {rules.max_entry_seconds}s"),
        Check("time_to_expiry",
              quote.seconds_to_expiry >= rules.min_seconds_to_expiry,
              f"{quote.seconds_to_expiry}s left >= {rules.min_seconds_to_expiry}s"),
        Check("market_not_decided", quote.skew <= rules.max_entry_skew,
              f"quote skew {quote.skew:.3f} <= {rules.max_entry_skew:.3f}"
              + ("" if quote.skew <= rules.max_entry_skew
                 else "; the market has already priced the move")),
        Check("priced_edge",
              side != FLAT and edge >= cfg.betting.required_edge,
              f"p_model {signal.p_model:.3f} - price {price:.3f} = {edge:+.3f} "
              f"vs required {cfg.betting.required_edge:+.3f}"
              if side != FLAT else "no side proposed"),
    ]
    approved = all(c.passed for c in conditions)
    blocked = tuple(c.label for c in conditions if not c.passed)

    decision = MarketDecision(
        quote=quote, side=side, p_model=signal.p_model,
        price=price if side != FLAT else float("nan"), edge=edge,
        expected_value_per_contract=ev_per_contract,
        conditions=tuple(conditions), approved=approved, blocked_by=blocked,
        other_side_edge=other_edge, gas_cost=rules.gas_cost_quote,
        signal_reason=("" if signal.tradable else signal._first_reason()),
    )
    if not approved or risk is None:
        return decision

    # Size against the odds this contract actually pays, not a nominal payout.
    contract_odds = (1.0 - price) / price if price > 0 else 0.0
    risk_decision = risk.assess(bar_index=signal.bar_index, ts=quote.observed_ts,
                                p_model=signal.p_model, atr_rank=atr_rank,
                                odds=contract_odds)
    decision.risk = risk_decision
    decision.approved = risk_decision.approved
    if not risk_decision.approved:
        decision.blocked_by = tuple(risk_decision.blocked_by)
        return decision

    decision.stake = risk_decision.stake
    decision.contracts = risk_decision.stake / price if price > 0 else 0.0

    if rules.min_order_quote and decision.stake < rules.min_order_quote:
        decision.approved = False
        decision.blocked_by = ("min_order",)
        decision.conditions = decision.conditions + (
            Check("min_order", False,
                  f"stake {decision.stake:,.2f} is below the venue minimum "
                  f"{rules.min_order_quote:,.2f}; raise the bankroll, the stake "
                  f"cap, or tighten the stack so it fires less often"),)
        return decision

    # Gas is charged per bet regardless of size, so on a small stake it can eat
    # the whole edge.  Refusing here is cheaper than discovering it in the P&L.
    if decision.expected_profit <= 0.0:
        decision.approved = False
        decision.blocked_by = ("gas_cost",)
        decision.conditions = decision.conditions + (
            Check("gas_cost", False,
                  f"expected gross {decision.contracts * ev_per_contract:,.2f} "
                  f"does not cover {rules.gas_cost_quote:,.2f} gas; "
                  f"stake more or skip"),)
    else:
        decision.conditions = decision.conditions + (
            Check("gas_cost", True,
                  f"expected gross {decision.contracts * ev_per_contract:,.2f} "
                  f"covers {rules.gas_cost_quote:,.2f} gas"),)
    return decision


def minimum_viable_stake(edge: float, price: float, rules: VenueRules,
                         margin: float = 3.0) -> float:
    """Smallest stake where expected profit is ``margin`` times the gas cost.

    Below this, you are paying the chain more than the edge is worth.
    """
    if edge <= 0.0 or not 0.0 < price < 1.0:
        return float("inf")
    ev_per_contract = edge          # p*(1-price) - (1-p)*price == p - price
    return margin * rules.gas_cost_quote * price / ev_per_contract
