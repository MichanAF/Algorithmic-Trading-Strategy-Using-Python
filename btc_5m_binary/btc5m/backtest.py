"""Event-driven backtest for the 5-minute binary up/down bet.

The loop is deliberately ordered so no bar can see its own future:

    for each bar i:
        1. settle every bet whose expiry bar is i
        2. evaluate the gate stack on bar i (closed data only)
        3. ask the risk manager for a stake, and open the bet

Settlement compares the close ``horizon_bars`` later against the entry close.
A move smaller than ``deadband_bps`` is a tie, handled per ``tie_policy`` --
worth setting honestly, because most venues resolve an exact tie against you.

The report includes a calibration table: realised hit rate per conviction
bucket, next to the probability the model claimed.  That table, not the P&L
line, is what tells you whether ``prob_cap`` is set honestly.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .config import StrategyConfig, to_dict
from .data import BarSeries
from .features import FeatureSet, build_features
from .gates import DOWN, UP
from .risk import OpenBet, RiskManager, SettledBet
from .signal import Signal, SignalEngine

BARS_PER_DAY = 288


@dataclass
class BacktestResult:
    config: dict
    bars: int
    bars_evaluated: int
    start_ts: int
    end_ts: int
    bets: list[SettledBet] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    starting_bankroll: float = 0.0
    final_bankroll: float = 0.0
    break_even: float = 0.0
    odds: float = 0.0
    signal_blocks: Counter = field(default_factory=Counter)
    risk_blocks: Counter = field(default_factory=Counter)
    gate_blocks: Counter = field(default_factory=Counter)
    tradable_signals: int = 0
    halted: bool = False
    halt_reason: str = ""

    # -- summary statistics --------------------------------------------- #

    @property
    def graded(self) -> list[SettledBet]:
        return [b for b in self.bets if b.outcome != "void"]

    @property
    def wins(self) -> int:
        return sum(1 for b in self.bets if b.outcome == "win")

    @property
    def losses(self) -> int:
        return sum(1 for b in self.bets if b.outcome == "loss")

    @property
    def voids(self) -> int:
        return sum(1 for b in self.bets if b.outcome == "void")

    @property
    def hit_rate(self) -> float | None:
        g = self.graded
        return self.wins / len(g) if g else None

    @property
    def net_pnl(self) -> float:
        return self.final_bankroll - self.starting_bankroll

    @property
    def return_pct(self) -> float:
        return self.net_pnl / self.starting_bankroll if self.starting_bankroll else 0.0

    @property
    def total_staked(self) -> float:
        return sum(b.stake for b in self.bets)

    @property
    def expectancy_per_bet(self) -> float | None:
        """Net P&L per unit staked -- the honest per-bet edge."""
        return self.net_pnl / self.total_staked if self.total_staked else None

    @property
    def max_drawdown(self) -> float:
        if not self.equity:
            return 0.0
        eq = np.asarray(self.equity, dtype=float)
        peak = np.maximum.accumulate(eq)
        with np.errstate(invalid="ignore", divide="ignore"):
            dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
        return float(np.max(dd))

    @property
    def longest_loss_streak(self) -> int:
        best = run = 0
        for b in self.bets:
            run = run + 1 if b.outcome == "loss" else 0
            best = max(best, run)
        return best

    @property
    def days(self) -> float:
        return max(1e-9, (self.end_ts - self.start_ts) / 86_400.0)

    @property
    def bets_per_day(self) -> float:
        return len(self.bets) / self.days

    @property
    def sharpe(self) -> float | None:
        """Annualised Sharpe of per-bet returns on stake."""
        if len(self.bets) < 2:
            return None
        r = np.array([b.pnl / b.stake for b in self.bets if b.stake > 0])
        if len(r) < 2 or r.std(ddof=1) == 0:
            return None
        return float(r.mean() / r.std(ddof=1) * math.sqrt(max(1.0, self.bets_per_day * 365)))

    @property
    def hit_rate_stderr(self) -> float | None:
        """Standard error on the hit rate -- the number that decides if N is enough."""
        g = self.graded
        if not g:
            return None
        p = self.wins / len(g)
        return math.sqrt(max(0.0, p * (1.0 - p)) / len(g))

    def edge_is_significant(self, z: float = 2.0) -> bool | None:
        """Is the hit rate above break-even by more than z standard errors?"""
        hr, se = self.hit_rate, self.hit_rate_stderr
        if hr is None or se is None or se == 0.0:
            return None
        return (hr - self.break_even) / se >= z

    def calibration(self, buckets: int = 4) -> list[dict]:
        """Realised hit rate per conviction bucket vs the claimed probability."""
        g = self.graded
        if not g:
            return []
        lo = min(b.conviction for b in g)
        hi = max(b.conviction for b in g)
        if hi - lo < 1e-9:
            edges = [lo, hi + 1e-9]
        else:
            edges = list(np.linspace(lo, hi + 1e-9, buckets + 1))
        rows = []
        for k in range(len(edges) - 1):
            lo_k, hi_k = edges[k], edges[k + 1]
            sel = [b for b in g if lo_k <= b.conviction < hi_k]
            if not sel:
                continue
            rows.append({
                "conviction": f"{lo_k:.2f}-{hi_k:.2f}",
                "bets": len(sel),
                "claimed_p": sum(b.p_model for b in sel) / len(sel),
                "realised_p": sum(1 for b in sel if b.outcome == "win") / len(sel),
                "pnl": sum(b.pnl for b in sel),
            })
        return rows

    # -- rendering ------------------------------------------------------ #

    def summary(self) -> str:
        hr = self.hit_rate
        exp = self.expectancy_per_bet
        sh = self.sharpe
        se = self.hit_rate_stderr
        sig = self.edge_is_significant()
        lines = [
            "=" * 66,
            f"BACKTEST  {self.config.get('name', '')}",
            "=" * 66,
            f"  bars in series        {self.bars:,} "
            f"({self.bars / BARS_PER_DAY:.1f} days)",
            f"  bars evaluated        {self.bars_evaluated:,}",
            f"  tradable signals      {self.tradable_signals:,}",
            f"  bets placed           {len(self.bets):,} "
            f"({self.bets_per_day:.2f}/day)",
            f"  wins / losses / void  {self.wins} / {self.losses} / {self.voids}",
            "  hit rate              "
            + (f"{hr:.2%}" + (f" +/- {se:.2%}" if se else "") if hr is not None else "n/a"),
            f"  break-even needed     {self.break_even:.2%} "
            f"(payout {self.odds:.3f}x)",
            "  edge vs break-even    "
            + (f"{hr - self.break_even:+.2%}" if hr is not None else "n/a")
            + (f"   [{'significant' if sig else 'NOT significant'} at 2 s.e.]"
               if sig is not None else ""),
            f"  net P&L               {self.net_pnl:+,.2f} "
            f"({self.return_pct:+.2%} on {self.starting_bankroll:,.0f})",
            f"  total staked          {self.total_staked:,.2f}",
            "  expectancy / stake    "
            + (f"{exp:+.4f}" if exp is not None else "n/a"),
            f"  max drawdown          {self.max_drawdown:.2%}",
            f"  longest loss streak   {self.longest_loss_streak}",
            "  annualised Sharpe     " + (f"{sh:.2f}" if sh is not None else "n/a"),
        ]
        if self.halted:
            lines.append(f"  HALTED                {self.halt_reason}")

        rows = self.calibration()
        if rows:
            lines += ["", "  calibration (does conviction predict anything?)",
                      "    conviction      bets   claimed    realised       P&L"]
            for r in rows:
                lines.append(
                    f"    {r['conviction']:>12s}  {r['bets']:6d}   "
                    f"{r['claimed_p']:7.2%}   {r['realised_p']:8.2%}  "
                    f"{r['pnl']:+9.2f}")

        if self.gate_blocks:
            lines += ["", "  first gate to reject a bar"]
            total = max(1, sum(self.gate_blocks.values()))
            for name, count in self.gate_blocks.most_common():
                lines.append(f"    {name:22s} {count:7,d}  {count / total:6.1%}")
        if self.signal_blocks:
            lines += ["", "  betting conditions that blocked a bar"]
            for name, count in self.signal_blocks.most_common():
                lines.append(f"    {name:22s} {count:7,d}")
        if self.risk_blocks:
            lines += ["", "  risk conditions that refused a signal"]
            for name, count in self.risk_blocks.most_common():
                lines.append(f"    {name:22s} {count:7,d}")
        lines.append("=" * 66)
        return "\n".join(lines)


def resolve_outcome(entry: float, exit_: float, side: int, deadband_bps: float,
                    tie_policy: str) -> str:
    """Did the bet win?  ``side`` is UP or DOWN; ties follow the venue's rule.

    Tie policies differ by venue and are not interchangeable.  predict.fun pays
    0.50 to both sides on an exact tie ("void" here).  Polymarket resolves Up
    when the end price is *greater than or equal to* the start, so a tie pays
    the UP side in full and costs the DOWN side everything ("favor_up").
    """
    if entry <= 0:
        return "void"
    move_bps = (exit_ - entry) / entry * 10_000.0
    if abs(move_bps) < deadband_bps or move_bps == 0.0:
        if tie_policy == "void":
            return "void"
        if tie_policy == "favor_up":
            return "win" if side == UP else "loss"
        if tie_policy == "favor_down":
            return "win" if side == DOWN else "loss"
        return "loss"
    direction = UP if move_bps > 0 else DOWN
    return "win" if direction == side else "loss"


def run_backtest(series: BarSeries, cfg: StrategyConfig,
                 reference: BarSeries | None = None,
                 features: FeatureSet | None = None,
                 on_signal=None,
                 minute: BarSeries | None = None) -> BacktestResult:
    """Walk the series bar by bar and return the full result.

    ``minute`` is the optional 1-minute series the taker-flow gate reads when
    its source is ``"1m"``; every other gate ignores it.
    """
    cfg.validate()
    engine = SignalEngine(cfg)
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    fs = (features if features is not None
          else build_features(series, cfg, reference, minute=minute))

    b = cfg.betting
    horizon = b.horizon_bars
    n = len(series)
    start = max(engine.warmup_bars(fs), 1)

    result = BacktestResult(
        config=to_dict(cfg), bars=n, bars_evaluated=0,
        start_ts=int(series.ts[start]) if start < n else 0,
        end_ts=int(series.ts[-1]) if n else 0,
        starting_bankroll=risk.bankroll, final_bankroll=risk.bankroll,
        break_even=engine.break_even, odds=engine.odds,
        equity=[risk.bankroll],
    )
    if start >= n:
        return result

    pending: dict[int, list[OpenBet]] = {}

    for i in range(start, n):
        ts = int(series.ts[i])

        # 1. settle everything expiring on this bar, before any new decision.
        for bet in pending.pop(i, []):
            exit_price = float(series.close[i])
            outcome = resolve_outcome(bet.entry_price, exit_price, bet.side,
                                      b.deadband_bps, b.tie_policy)
            record = risk.settle(bet, exit_price, outcome, exit_ts=ts)
            result.bets.append(record)
            result.equity.append(risk.bankroll)

        if i + horizon >= n:
            continue

        # 2. gates and betting conditions.
        result.bars_evaluated += 1
        signal: Signal = engine.evaluate(fs, i)
        if on_signal is not None:
            on_signal(signal)

        first_reject = next((g.name for g in signal.gate_results if not g.passed), None)
        if first_reject:
            result.gate_blocks[first_reject] += 1
        for label in signal.blocked_by:
            result.signal_blocks[label] += 1
        if not signal.tradable:
            continue
        result.tradable_signals += 1

        # 3. risk conditions and stake.
        atr_rank = fs.values["atr_rank"][i]
        decision = risk.assess(bar_index=i, ts=ts, p_model=signal.p_model,
                               atr_rank=float(atr_rank) if np.isfinite(atr_rank) else None)
        if not decision.approved:
            for label in decision.blocked_by:
                result.risk_blocks[label] += 1
            continue

        expiry_bar = i + horizon
        bet = OpenBet(
            bar_index=i, ts=ts, side=signal.side, stake=decision.stake,
            entry_price=signal.price,
            expiry_ts=ts + horizon * b.bar_seconds, expiry_bar=expiry_bar,
            p_model=signal.p_model, conviction=signal.conviction,
        )
        risk.open(bet)
        pending.setdefault(expiry_bar, []).append(bet)

    # Any bet still open at the end of the series never resolved; drop it
    # rather than guess, and say so through the bet count.
    result.final_bankroll = risk.bankroll
    result.halted = risk.halted or risk.health_halted
    result.halt_reason = risk.halt_reason or risk.health_halt_reason
    return result
