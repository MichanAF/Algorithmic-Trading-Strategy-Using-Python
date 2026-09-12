"""The three risk-management conditions.

A binary bet cannot be stopped out.  Once placed, the whole stake is at risk
until expiry, so every risk control has to act *before* the bet exists.  That
leaves exactly three levers, and this module is all three:

1. **Stake sizing** -- fractional Kelly on the modelled edge, hard-capped as a
   percentage of bankroll.  Kelly sizes the bet to the edge; the cap is what
   survives the edge being overestimated, which it will be.
2. **Loss limits and exposure** -- daily loss cap, peak-to-trough drawdown stop,
   consecutive-loss cooldown, bets per hour, concurrent bets.  These bound the
   damage from a bad run and from the correlation between back-to-back
   five-minute bets.
3. **Strategy health** -- rolling hit rate measured against the break-even the
   payout actually implies, plus a volatility-shock veto.  This is the control
   that notices the edge has stopped existing, which no amount of position
   sizing will save you from.

Every condition returns a ``Check`` so a refused bet is as traceable as a
refused gate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import RiskConfig
from .gates import Check


@dataclass
class RiskDecision:
    approved: bool
    stake: float
    kelly_fraction: float
    stake_pct: float
    conditions: tuple[Check, ...] = ()
    blocked_by: tuple[str, ...] = field(default_factory=tuple)
    derated: bool = False

    def report(self) -> str:
        lines = ["risk conditions:"]
        lines.extend(f"  {c}" for c in self.conditions)
        verdict = "APPROVED" if self.approved else "REFUSED"
        lines.append(f"=> {verdict} stake {self.stake:,.2f} "
                     f"({self.stake_pct:.2%} of bankroll)"
                     + ("  [derated: health]" if self.derated else ""))
        if self.blocked_by:
            lines.append(f"   blocked by: {', '.join(self.blocked_by)}")
        return "\n".join(lines)


@dataclass
class OpenBet:
    bar_index: int
    ts: int
    side: int
    stake: float
    entry_price: float
    expiry_ts: int
    expiry_bar: int
    p_model: float
    conviction: float


@dataclass
class SettledBet:
    bar_index: int
    ts: int
    side: int
    stake: float
    entry_price: float
    exit_price: float
    pnl: float
    outcome: str          # "win" | "loss" | "void"
    p_model: float
    conviction: float
    bankroll_after: float


class RiskManager:
    """Stateful bankroll, exposure and health tracking across a run."""

    def __init__(self, cfg: RiskConfig, break_even: float, odds: float):
        if not 0.0 < break_even < 1.0:
            raise ValueError("break_even must be in (0, 1)")
        if odds <= 0.0:
            raise ValueError("odds must be positive")
        self.cfg = cfg
        self.break_even = break_even
        self.odds = odds

        self.bankroll = float(cfg.starting_bankroll)
        self.peak_bankroll = self.bankroll
        self.day_key: str | None = None
        self.day_start_bankroll = self.bankroll

        self.consecutive_losses = 0
        self.cooldown_until_bar = -1
        self.halted = False
        self.halt_reason = ""

        self.open_bets: list[OpenBet] = []
        self.settled: list[SettledBet] = []
        self.recent_outcomes: deque[int] = deque(maxlen=max(1, cfg.health_window))
        self.recent_bet_ts: deque[int] = deque()

    # ------------------------------------------------------------------ #
    # bookkeeping
    # ------------------------------------------------------------------ #

    @staticmethod
    def _day_key(ts: int) -> str:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")

    def roll_clock(self, ts: int) -> None:
        """Advance the UTC day, resetting the daily loss budget."""
        key = self._day_key(ts)
        if key != self.day_key:
            self.day_key = key
            self.day_start_bankroll = self.bankroll
        while self.recent_bet_ts and ts - self.recent_bet_ts[0] >= 3600:
            self.recent_bet_ts.popleft()

    @property
    def day_pnl(self) -> float:
        return self.bankroll - self.day_start_bankroll

    @property
    def drawdown(self) -> float:
        if self.peak_bankroll <= 0.0:
            return 0.0
        return (self.peak_bankroll - self.bankroll) / self.peak_bankroll

    @property
    def exposure(self) -> float:
        return sum(b.stake for b in self.open_bets)

    def rolling_hit_rate(self) -> float | None:
        graded = [o for o in self.recent_outcomes if o >= 0]
        if len(graded) < self.cfg.health_min_samples:
            return None
        return sum(graded) / len(graded)

    # ------------------------------------------------------------------ #
    # the three conditions
    # ------------------------------------------------------------------ #

    def assess(self, *, bar_index: int, ts: int, p_model: float,
               atr_rank: float | None = None,
               odds: float | None = None) -> RiskDecision:
        """Run all three risk conditions and return a stake, or a refusal.

        ``odds`` overrides the configured payoff for this one bet.  On a
        contract-priced venue the payoff changes with every quote -- a side
        bought at 0.51 pays 0.96 to 1, one bought at 0.40 pays 1.50 to 1 -- and
        sizing them all at a nominal fixed payout would misstate Kelly on every
        bet.
        """
        self.roll_clock(ts)
        health, derate = self._health_condition(atr_rank)
        limits = self._limit_conditions(bar_index, ts)
        kelly, stake, stake_pct, sizing = self._stake_condition(
            p_model, derate, odds if odds is not None else self.odds)

        conditions = (sizing, *limits, health)
        approved = all(x.passed for x in conditions if x.applicable)
        blocked = tuple(x.label for x in conditions
                        if x.applicable and not x.passed)
        return RiskDecision(
            approved=approved, stake=stake if approved else 0.0,
            kelly_fraction=kelly, stake_pct=stake_pct if approved else 0.0,
            conditions=conditions, blocked_by=blocked, derated=derate < 1.0,
        )

    # -- condition 1: stake sizing -------------------------------------- #

    def _stake_condition(self, p_model: float, derate: float,
                         odds: float) -> tuple[float, float, float, Check]:
        c = self.cfg
        if odds <= 0.0:
            raise ValueError("odds must be positive")
        # Kelly for a binary payoff: f* = (p*b - (1-p)) / b
        kelly = (p_model * odds - (1.0 - p_model)) / odds
        fraction = max(0.0, kelly) * c.kelly_fraction * derate
        capped = min(fraction, c.max_stake_pct)

        raw = self.bankroll * capped
        stake = 0.0
        if c.stake_rounding > 0:
            stake = (raw // c.stake_rounding) * c.stake_rounding
        else:
            stake = raw
        if stake < c.min_stake <= raw:
            stake = c.min_stake

        affordable = self.bankroll * c.max_stake_pct
        ok = (kelly > 0.0 and stake >= c.min_stake
              and stake <= affordable + 1e-9
              and stake + self.exposure <= self.bankroll)
        # Say which of the two halves actually set the size.  At realistic
        # edges the cap binds at every conviction, which means kelly_fraction
        # is not sizing anything -- worth knowing before tuning it.
        bound_by = "cap" if fraction > c.max_stake_pct else "Kelly"
        detail = (f"Kelly {kelly:.3f} at {odds:.3f}x x {c.kelly_fraction} "
                  + (f"x derate {derate:.2f} " if derate < 1.0 else "")
                  + f"-> {capped:.2%} of bankroll "
                  f"(set by {bound_by}; cap {c.max_stake_pct:.2%}), "
                  f"stake {stake:,.2f}")
        if kelly <= 0.0:
            detail = f"Kelly {kelly:.3f} <= 0: no edge at these odds"
        elif stake < c.min_stake:
            detail += f"; below minimum stake {c.min_stake:,.2f}"
        return kelly, stake, (stake / self.bankroll if self.bankroll else 0.0), \
            Check("stake_sizing", ok, detail)

    # -- condition 2: loss limits and exposure -------------------------- #

    def _limit_conditions(self, bar_index: int, ts: int) -> tuple[Check, ...]:
        c = self.cfg
        daily_budget = self.day_start_bankroll * c.daily_loss_limit_pct
        bets_this_hour = len(self.recent_bet_ts)
        # The halt flag belongs to max_drawdown alone.  Folding it into the daily
        # check as well made both conditions fail on the same bars, so the
        # blocker histogram double-counted every post-halt bar and neither
        # number meant anything on its own.
        return (
            Check("daily_loss_limit", self.day_pnl > -daily_budget,
                  f"day P&L {self.day_pnl:+,.2f} vs limit "
                  f"-{daily_budget:,.2f} ({c.daily_loss_limit_pct:.1%})"),
            Check("max_drawdown", not self.halted and self.drawdown < c.max_drawdown_pct,
                  f"drawdown {self.drawdown:.2%} < {c.max_drawdown_pct:.2%}"
                  + (f" [HALTED: {self.halt_reason}]" if self.halted else "")),
            Check("loss_streak_cooldown", bar_index >= self.cooldown_until_bar,
                  f"{self.consecutive_losses} consecutive loss(es), "
                  f"cooldown until bar {max(0, self.cooldown_until_bar)}"),
            Check("bets_per_hour", bets_this_hour < c.max_bets_per_hour,
                  f"{bets_this_hour} bet(s) in the last hour < "
                  f"{c.max_bets_per_hour}"),
            Check("concurrent_bets", len(self.open_bets) < c.max_concurrent_bets,
                  f"{len(self.open_bets)} open bet(s) < {c.max_concurrent_bets}"),
        )

    # -- condition 3: strategy health ----------------------------------- #

    def _health_condition(self, atr_rank: float | None) -> tuple[Check, float]:
        """Returns the condition plus a stake multiplier (1.0, or the derate)."""
        c = self.cfg
        hit = self.rolling_hit_rate()
        floor = self.break_even - c.hit_rate_buffer
        derate = 1.0
        parts: list[str] = []

        healthy = True
        if hit is None:
            parts.append(f"hit rate: only {len([o for o in self.recent_outcomes if o >= 0])} "
                         f"graded bet(s), need {c.health_min_samples} to judge")
        else:
            parts.append(f"rolling hit rate {hit:.1%} vs floor {floor:.1%} "
                         f"(break-even {self.break_even:.1%} "
                         f"- buffer {c.hit_rate_buffer:.1%})")
            if hit < floor:
                if c.halt_when_unhealthy:
                    healthy = False
                else:
                    derate = c.unhealthy_stake_derate
                    parts.append(f"derating stake to {derate:.0%}")

        if atr_rank is not None:
            parts.append(f"ATR rank {atr_rank:.3f} vs shock {c.atr_shock_rank:.3f}")
            if atr_rank >= c.atr_shock_rank:
                healthy = False
                parts.append("volatility shock: standing down")

        return Check("strategy_health", healthy, "; ".join(parts)), derate

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def open(self, bet: OpenBet) -> None:
        if bet.stake <= 0.0:
            raise ValueError("cannot open a bet with a non-positive stake")
        self.open_bets.append(bet)
        self.recent_bet_ts.append(bet.ts)

    def settle(self, bet: OpenBet, exit_price: float, outcome: str,
               exit_ts: int | None = None) -> SettledBet:
        """Book the result of a bet and update every piece of risk state."""
        if outcome not in ("win", "loss", "void"):
            raise ValueError(f"unknown outcome {outcome!r}")
        if bet in self.open_bets:
            self.open_bets.remove(bet)

        if outcome == "win":
            pnl = bet.stake * self.odds
        elif outcome == "loss":
            pnl = -bet.stake
        else:
            pnl = 0.0

        self.bankroll += pnl
        self.peak_bankroll = max(self.peak_bankroll, self.bankroll)
        self.roll_clock(exit_ts if exit_ts is not None else bet.expiry_ts)

        if outcome == "win":
            self.consecutive_losses = 0
            self.recent_outcomes.append(1)
        elif outcome == "loss":
            self.consecutive_losses += 1
            self.recent_outcomes.append(0)
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until_bar = bet.expiry_bar + self.cfg.cooldown_bars
        else:
            self.recent_outcomes.append(-1)

        if self.drawdown >= self.cfg.max_drawdown_pct and not self.halted:
            self.halted = True
            self.halt_reason = (f"drawdown {self.drawdown:.2%} hit the "
                                f"{self.cfg.max_drawdown_pct:.2%} limit")

        record = SettledBet(
            bar_index=bet.bar_index, ts=bet.ts, side=bet.side, stake=bet.stake,
            entry_price=bet.entry_price, exit_price=exit_price, pnl=pnl,
            outcome=outcome, p_model=bet.p_model, conviction=bet.conviction,
            bankroll_after=self.bankroll,
        )
        self.settled.append(record)
        return record
