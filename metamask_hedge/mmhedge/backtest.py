"""Walk the book forward hour by hour, paying every fee on the way.

What this simulates, honestly:

* hourly funding, credited on ``qty * oracle_price * rate`` the way Hyperliquid
  pays it -- to the margin account, not compounded into the position
* mark-to-market on the short, hour by hour
* **isolated margin**: the short is liquidated on its own collateral, and the
  core's gain in that same rally does not save it
* every fee: maker or taker on each clip of hedge notional, the spot swap if
  the core is ever traded, the withdrawal when collateral comes home
* the opportunity cost of collateral, which earns nothing on HyperCore and
  nothing staged on Arbitrum, against mUSD that pays

What it does not simulate, and you should not let it flatter you: slippage
beyond the configured budget, maker orders that fail to fill (the default
assumes they fill, which is optimistic and worth roughly 0.09% a round trip),
oracle/mark divergence, venue downtime, and the fact that one price series is
being asked to stand in for a multi-asset core.

The comparison that matters is not "did it make money". It is the three
columns: the core alone, the core hedged by this policy, and the core pinned
delta-neutral forever. A carry strategy that beats HODL in a bear year and
loses to it in a bull year has not found an edge; it has found a short.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import StrategyConfig
from .data import Series
from .hedge import MarketState, decide
from .sizing import Allocation, allocate, liquidation_move, margin_fraction_for_survival

HOURS_PER_YEAR = 24 * 365


def _ema(x: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rolling_rank(x: np.ndarray, window: int) -> np.ndarray:
    """Percentile of each point within its trailing window."""
    out = np.full(len(x), 0.5)
    for i in range(len(x)):
        lo = max(0, i - window + 1)
        past = x[lo:i + 1]
        if len(past) > 8:
            out[i] = float(np.mean(past <= x[i]))
    return out


@dataclass
class Signals:
    """Everything the policy reads, precomputed once."""

    trend: np.ndarray
    vol_rank: np.ndarray
    funding_trailing: np.ndarray        # annualised
    negative_hours: np.ndarray


def build_signals(series: Series, cfg: StrategyConfig, *, fast_hours: int = 72,
                  slow_hours: int = 336, vol_window: int = 168,
                  rank_window: int = 24 * 90) -> Signals:
    price, funding = series.price, series.funding
    n = len(price)

    fast, slow = _ema(price, fast_hours), _ema(price, slow_hours)
    log_ret = np.diff(np.log(price), prepend=np.log(price[0]))
    vol = np.array([log_ret[max(0, i - vol_window + 1):i + 1].std() or 1e-9
                    for i in range(n)])
    # Separation between the two EMAs, in units of the move the asset makes
    # over the slow window.  Dividing by vol is what stops a quiet uptrend and
    # a violent one from scoring the same.
    spread = (fast - slow) / price
    scale = vol * np.sqrt(slow_hours)
    trend = np.clip(spread / np.maximum(scale, 1e-9), -1.0, 1.0)

    vol_rank = _rolling_rank(vol, rank_window)

    look = max(1, cfg.carry.lookback_hours)
    csum = np.cumsum(np.insert(funding, 0, 0.0))
    idx = np.arange(n)
    lo = np.maximum(0, idx - look + 1)
    trailing = (csum[idx + 1] - csum[lo]) / (idx + 1 - lo) * HOURS_PER_YEAR

    neg = np.zeros(n, dtype=int)
    run = 0
    for i in range(n):
        run = run + 1 if funding[i] < 0.0 else 0
        neg[i] = run

    return Signals(trend=trend, vol_rank=vol_rank, funding_trailing=trailing,
                   negative_hours=neg)


@dataclass
class BacktestResult:
    label: str
    ts: np.ndarray
    equity: np.ndarray
    core_value: np.ndarray
    hedge_ratio: np.ndarray
    perp_cash: np.ndarray
    funding_collected: float
    fees_paid: float
    yield_earned: float
    liquidations: int
    top_ups: int
    deleverages: int
    turnover_usd: float
    trades: int
    capital: float
    alloc: Allocation | None = None
    events: tuple[str, ...] = field(default_factory=tuple)

    # -- metrics --------------------------------------------------------- #

    @property
    def years(self) -> float:
        return len(self.equity) / HOURS_PER_YEAR

    @property
    def total_return(self) -> float:
        return self.equity[-1] / self.equity[0] - 1.0

    @property
    def cagr(self) -> float:
        if self.years <= 0 or self.equity[0] <= 0 or self.equity[-1] <= 0:
            return 0.0
        return (self.equity[-1] / self.equity[0]) ** (1.0 / self.years) - 1.0

    @property
    def ann_vol(self) -> float:
        r = np.diff(np.log(np.maximum(self.equity, 1e-9)))
        return float(r.std() * np.sqrt(HOURS_PER_YEAR)) if len(r) else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = np.maximum.accumulate(self.equity)
        return float(np.max((peak - self.equity) / np.maximum(peak, 1e-9)))

    @property
    def sharpe(self) -> float:
        return self.cagr / self.ann_vol if self.ann_vol > 1e-9 else 0.0

    @property
    def avg_hedge_ratio(self) -> float:
        return float(self.hedge_ratio.mean())

    def row(self) -> str:
        return (f"{self.label:<22} {self.total_return:>+8.1%} {self.cagr:>+8.1%} "
                f"{self.ann_vol:>8.1%} {self.max_drawdown:>8.1%} {self.sharpe:>7.2f} "
                f"{self.avg_hedge_ratio:>7.2f} {self.funding_collected:>11,.0f} "
                f"{self.fees_paid:>9,.0f} {self.liquidations:>5} "
                f"{self.deleverages:>5}")

    def summary(self) -> str:
        return "\n".join([
            f"{self.label}: ${self.capital:,.0f} over {self.years:.2f} years",
            f"  return      {self.total_return:+.2%}  (CAGR {self.cagr:+.2%})",
            f"  volatility  {self.ann_vol:.2%} annualised",
            f"  max DD      {self.max_drawdown:.2%}",
            f"  return/vol  {self.sharpe:.2f}",
            f"  hedge       {self.avg_hedge_ratio:.2f} average, "
            f"{self.trades} adjustments, {self.turnover_usd:,.0f} USD turned over",
            f"  funding     {self.funding_collected:+,.0f}",
            f"  fees        {self.fees_paid:-,.0f}",
            f"  cash yield  {self.yield_earned:+,.0f}",
            f"  liquidations {self.liquidations}, top-ups {self.top_ups}, "
            f"forced deleverages {self.deleverages}",
        ])


HEADER = (f"{'strategy':<22} {'return':>8} {'CAGR':>8} {'vol':>8} {'maxDD':>8} "
          f"{'ret/vol':>7} {'avg h':>7} {'funding':>11} {'fees':>9} {'liq':>5} "
          f"{'delev':>5}")


def render_table(results) -> str:
    return "\n".join([HEADER, "-" * len(HEADER)] + [r.row() for r in results])


def run_backtest(series: Series, cfg: StrategyConfig, *, policy: str = "adaptive",
                 capital_usd: float | None = None, decision_hours: int = 24,
                 signals: Signals | None = None,
                 cash_floor_pct: float = 0.10) -> BacktestResult:
    """Run one policy over one price/funding series.

    ``policy`` is ``"adaptive"`` (the hedge module decides), ``"neutral"``
    (pinned at ``max_hedge_ratio`` throughout) or ``"none"`` (core only).

    The single-series simplification: this treats ``series`` as the entire core
    and uses that symbol's margin tier. A three-asset core needs three series
    and a correlation model, which would add machinery without changing any of
    the conclusions the comparison is here to test.
    """
    if policy not in ("adaptive", "neutral", "none"):
        raise ValueError("policy must be adaptive, neutral or none")
    signals = signals or build_signals(series, cfg)
    alloc = allocate(cfg, capital_usd, cash_floor_pct=cash_floor_pct)

    venue, price, funding = cfg.venue, series.price, series.funding
    n = len(price)
    maint = venue.tier_for(series.symbol).maintenance_margin
    lev = cfg.hedge.leverage
    fee_rate = venue.perp_maker_pct if cfg.hedge.use_maker_orders else venue.perp_taker_pct
    musd_hourly = venue.musd_apy / HOURS_PER_YEAR
    ceiling = cfg.risk.survive_rally_pct

    core_qty = alloc.core_usd / price[0]
    # Collateral that is not currently posted earns in mUSD, so the whole
    # un-deployed balance starts there rather than idling on Arbitrum.
    musd = alloc.cash_usd + alloc.posted_usd + alloc.reserve_usd
    perp_cash = reserve = 0.0
    q = 0.0                      # coins short
    h = 0.0
    hedge_entry_price = 0.0
    hedge_age_hours = 0.0

    equity = np.empty(n)
    core_series = np.empty(n)
    h_series = np.empty(n)
    cash_series = np.empty(n)

    funding_total = fees_total = yield_total = turnover = 0.0
    liquidations = top_ups = trades = deleverages = 0
    cooldown_until = -1
    events: list[str] = []
    peak_core = alloc.core_usd

    def post_required(target_q: float, px: float) -> float:
        return target_q * px / lev

    def reserve_required(target_q: float, px: float) -> float:
        need = margin_fraction_for_survival(ceiling, maint) * target_q * px
        return max(0.0, need - post_required(target_q, px))

    for i in range(n):
        px = price[i]

        # 1. mark to market, then funding, then yield on idle cash
        if i > 0 and q > 0.0:
            perp_cash += q * (price[i - 1] - px)
            pay = q * px * funding[i]
            perp_cash += pay
            funding_total += pay
            hedge_age_hours += 1.0
        earned = musd * musd_hourly
        musd += earned
        yield_total += earned

        # 2. margin ladder: top up before the buffer is spent
        if q > 0.0 and perp_cash > 0.0 and hedge_entry_price > 0.0:
            bare_liq = liquidation_move(q * px / perp_cash, maint)
            move_so_far = (px - hedge_entry_price) / hedge_entry_price
            spent = move_so_far / bare_liq if bare_liq > 0.0 else 0.0
            if spent >= cfg.risk.margin_urgent_at:
                remaining = max(0.0, (ceiling - move_so_far) / (1.0 + move_so_far))
                need = margin_fraction_for_survival(remaining, maint) * q * px
                gap = need - perp_cash
                if gap > 0.0:
                    move_in = min(gap, reserve + musd)
                    from_reserve = min(reserve, move_in)
                    reserve -= from_reserve
                    musd -= (move_in - from_reserve)
                    perp_cash += move_in
                    top_ups += 1

        # 3. last resort: trim the short rather than die with it.
        #    Cutting notional fixes the margin ratio directly and costs 0.035%.
        #    Selling core to fund the same gap would cost 0.875% -- twenty-five
        #    times more -- to defend a hedge the market has already priced out.
        if (q > 0.0 and cfg.risk.margin_last_resort == "deleverage"
                and perp_cash < maint * q * px * cfg.risk.deleverage_trigger):
            safe_frac = margin_fraction_for_survival(
                cfg.risk.deleverage_target_survival, maint)
            # Solve for the trim *including* the fee it costs, which comes out
            # of the very collateral being defended.  Sizing the trim first and
            # paying for it afterwards can leave the remaining position with
            # less margin than it started with -- and liquidate the survivor.
            #   c - f*P*(q - t) >= safe * P * t
            #   t <= (c - f*P*q) / (P * (safe - f))
            f = abs(venue.perp_taker_pct)
            numer = perp_cash - f * px * q
            denom = px * (safe_frac - f)
            target_q = max(0.0, numer / denom) if denom > 0.0 and px > 0.0 else 0.0
            target_q = min(target_q, q)
            clip = (q - target_q) * px
            if clip > 0.0:
                cost = clip * f                           # forced: pay the taker
                perp_cash -= cost
                fees_total += cost
                turnover += clip
                trades += 1
                deleverages += 1
                events.append(
                    f"h={i} forced deleverage at {px:,.0f}: "
                    f"hedge {q * px:,.0f} -> {target_q * px:,.0f} notional")
                q = target_q
                h = q / core_qty if core_qty else 0.0
                cooldown_until = i + cfg.risk.deleverage_cooldown_hours
                # Belt and braces: if the trim still cannot be supported, close
                # the rest rather than leave a position the next bar will kill.
                if q > 0.0 and perp_cash < maint * q * px:
                    perp_cash -= q * px * f
                    fees_total += q * px * f
                    turnover += q * px
                    q, h = 0.0, 0.0
                if q == 0.0:
                    musd += max(0.0, perp_cash) + reserve
                    perp_cash = reserve = 0.0
                    hedge_entry_price, hedge_age_hours = 0.0, 0.0

        # 4. isolated-margin liquidation.  The core is up in exactly the move
        #    that kills the short, and cannot help it.
        if q > 0.0 and perp_cash < maint * q * px:
            lost = max(0.0, perp_cash)
            events.append(
                f"h={i} liquidated: price {px:,.0f}, "
                f"{lost:,.0f} of collateral gone, core left unhedged")
            perp_cash = 0.0
            q, h, hedge_age_hours = 0.0, 0.0, 0.0
            liquidations += 1
            musd += reserve
            reserve = 0.0

        # 5. the decision
        if policy != "none" and i % decision_hours == 0:
            cooling = i < cooldown_until
            if policy == "neutral":
                # Even the naive benchmark honours the cooldown; without it the
                # comparison measures churn rather than the policy.
                target = h if cooling else cfg.hedge.max_hedge_ratio
            else:
                state = MarketState(
                    funding_apr=float(signals.funding_trailing[i]),
                    funding_now_apr=float(funding[i] * HOURS_PER_YEAR),
                    negative_funding_hours=int(signals.negative_hours[i]),
                    trend_score=float(signals.trend[i]),
                    vol_rank=float(signals.vol_rank[i]),
                    core_drawdown=max(0.0, 1.0 - (core_qty * px) / peak_core),
                    current_ratio=h, days_held=hedge_age_hours / 24.0,
                    core_usd=core_qty * px, cooldown_active=cooling)
                target = decide(state, cfg, venue).applied_ratio

            if abs(target - h) > 1e-9:
                new_q = target * core_qty
                clip_notional = abs(new_q - q) * px
                cost = clip_notional * fee_rate
                perp_cash -= cost
                fees_total += cost
                turnover += clip_notional
                trades += 1

                if new_q > q:
                    # scaling in: (re-)anchor the entry at the blended price
                    hedge_entry_price = (px if q == 0.0
                                         else (hedge_entry_price * q + px * (new_q - q)) / new_q)
                    want_post = post_required(new_q, px)
                    want_reserve = reserve_required(new_q, px)
                    need_post = max(0.0, want_post - perp_cash)
                    take = min(need_post, musd)
                    musd -= take
                    perp_cash += take
                    need_res = max(0.0, want_reserve - reserve)
                    take_r = min(need_res, musd)
                    musd -= take_r
                    reserve += take_r
                elif new_q < q:
                    if new_q == 0.0:
                        # bring it home: one withdrawal fee, USDC on Arbitrum
                        if perp_cash > 0.0:
                            perp_cash -= venue.perp_withdrawal_fee_usd
                            fees_total += venue.perp_withdrawal_fee_usd
                        musd += max(0.0, perp_cash) + reserve
                        perp_cash = reserve = 0.0
                        hedge_entry_price, hedge_age_hours = 0.0, 0.0
                    else:
                        # sweep collateral freed by the smaller position
                        excess = perp_cash - post_required(new_q, px)
                        if excess > 0.0:
                            perp_cash -= excess
                            musd += excess
                        excess_r = reserve - reserve_required(new_q, px)
                        if excess_r > 0.0:
                            reserve -= excess_r
                            musd += excess_r
                q, h = new_q, target

        core_value = core_qty * px
        peak_core = max(peak_core, core_value)
        equity[i] = core_value + perp_cash + reserve + musd
        core_series[i] = core_value
        h_series[i] = h
        cash_series[i] = perp_cash

    label = {"adaptive": "hedged overlay", "neutral": "always delta-neutral",
             "none": "core only (HODL)"}[policy]
    return BacktestResult(
        label=label, ts=series.ts, equity=equity, core_value=core_series,
        hedge_ratio=h_series, perp_cash=cash_series,
        funding_collected=funding_total, fees_paid=fees_total,
        yield_earned=yield_total, liquidations=liquidations, top_ups=top_ups,
        deleverages=deleverages,
        turnover_usd=turnover, trades=trades, capital=alloc.capital_usd,
        alloc=alloc, events=tuple(events),
    )


def compare(series: Series, cfg: StrategyConfig, **kwargs) -> list[BacktestResult]:
    """The three columns that answer whether the overlay is worth running."""
    signals = build_signals(series, cfg)
    return [run_backtest(series, cfg, policy=p, signals=signals, **kwargs)
            for p in ("none", "adaptive", "neutral")]
