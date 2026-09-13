"""Typed configuration for the 5-minute BTC binary strategy.

Every threshold the strategy leans on lives here rather than in the gate code,
so a tuning run is a config diff and never a code diff.  Configs load from JSON
or YAML and are merged over the defaults, so an override file only needs to
carry the handful of keys it actually changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Sequence

# --------------------------------------------------------------------------- #
# gate parameters -- one dataclass per gate in the menu
# --------------------------------------------------------------------------- #


@dataclass
class DataIntegrityParams:
    """Gate 0: is the data itself fit to bet on?"""
    max_gap_bars: float = 1.5          # tolerated spacing vs nominal bar, in bars
    max_spread_bps: float = 6.0        # quoted spread ceiling when spread data exists
    require_positive_volume: bool = True
    max_stale_bars: int = 2            # identical closes in a row -> feed is stuck


@dataclass
class VolatilityRegimeParams:
    """Is there enough movement to pay for the spread, without being chaos?"""
    atr_period: int = 14
    rank_window: int = 288             # one day of 5m bars
    min_rank: float = 0.30
    max_rank: float = 0.92
    min_atr_bps: float = 8.0           # ATR floor in basis points of price
    bb_period: int = 20
    max_bb_width_rank: float = 0.97


@dataclass
class TrendAlignmentParams:
    """Which way, and does the higher timeframe agree?"""
    fast: int = 9
    mid: int = 21
    slow: int = 50
    htf_factor: int = 3                # 3 x 5m = 15m
    htf_ema: int = 21
    htf_slope_lookback: int = 2
    r2_window: int = 20
    min_r2: float = 0.20
    min_slope_atr_frac: float = 0.04   # |slope|/ATR per bar
    require_vwap_side: bool = True
    vwap_window: int = 24


@dataclass
class MomentumThrustParams:
    """Is it moving right now, and is there room left in the move?"""
    roc_bars: int = 3
    roc_z_window: int = 96
    min_roc_z: float = 0.70
    rsi_period: int = 14
    rsi_long_min: float = 50.0
    rsi_long_max: float = 78.0         # above this, the thrust is already spent
    rsi_short_min: float = 22.0
    rsi_short_max: float = 50.0
    require_macd_expansion: bool = True
    min_body_dominance: float = 0.20


@dataclass
class ParticipationParams:
    """Is real volume behind the move, or is it a thin drift?"""
    volume_window: int = 48
    min_volume_ratio: float = 1.10     # bar volume vs rolling median
    obv_slope_lookback: int = 5
    require_obv_agreement: bool = True
    max_volume_ratio: float = 6.0      # blow-off bar -> exhaustion, not thrust


@dataclass
class LocationParams:
    """Is there room to the next barrier, and are we chasing?"""
    swing_lookback: int = 24
    min_room_atr: float = 0.25         # distance to opposing swing, in ATR
    max_extension_atr: float = 2.25    # distance from VWAP, in ATR
    vwap_window: int = 24


@dataclass
class SessionParams:
    """Clock-based vetoes: liquidity windows and scheduled flow."""
    allowed_hours_utc: list[int] = field(default_factory=lambda: list(range(24)))
    funding_hours_utc: list[int] = field(default_factory=lambda: [0, 8, 16])
    funding_blackout_minutes: int = 5
    skip_weekend: bool = False


@dataclass
class PersistenceParams:
    """Does this regime continue moves, or reverse them?"""
    vr_window: int = 60
    vr_q: int = 5
    min_variance_ratio: float = 1.00
    adx_period: int = 14
    min_adx: float = 18.0


@dataclass
class MeanReversionParams:
    """The opposite personality: fade a stretched, exhausted move.

    Kept deliberately strict.  Loosening these thresholds raises the signal
    count and makes the measured edge worse, not better -- on the bundled test
    fixture, dropping to z >= 1.2 turns a statistically silent +5.6% into a
    significant -4.3%.  Treat this gate as unproven until `btc5m gates` says
    otherwise on your own history.

    Note it cannot be combined with ``persistence``: this gate requires a
    mean-reverting variance ratio and that one requires a trending ratio, so a
    stack holding both never fires. That is by construction, not a bug.
    """
    z_window: int = 60
    min_abs_z: float = 1.8
    exhaustion_volume_ratio: float = 1.5
    rsi_period: int = 14
    rsi_overbought: float = 72.0
    rsi_oversold: float = 28.0
    max_variance_ratio: float = 1.00


@dataclass
class CrossAssetParams:
    """Confirmation from a correlated series (ETH by default)."""
    roc_bars: int = 3
    min_abs_roc_bps: float = 3.0
    require_same_direction: bool = True


@dataclass
class GateParams:
    data_integrity: DataIntegrityParams = field(default_factory=DataIntegrityParams)
    volatility_regime: VolatilityRegimeParams = field(default_factory=VolatilityRegimeParams)
    trend_alignment: TrendAlignmentParams = field(default_factory=TrendAlignmentParams)
    momentum_thrust: MomentumThrustParams = field(default_factory=MomentumThrustParams)
    participation: ParticipationParams = field(default_factory=ParticipationParams)
    location: LocationParams = field(default_factory=LocationParams)
    session: SessionParams = field(default_factory=SessionParams)
    persistence: PersistenceParams = field(default_factory=PersistenceParams)
    mean_reversion: MeanReversionParams = field(default_factory=MeanReversionParams)
    cross_asset: CrossAssetParams = field(default_factory=CrossAssetParams)


# --------------------------------------------------------------------------- #
# the three betting conditions
# --------------------------------------------------------------------------- #


@dataclass
class BettingConfig:
    """Condition 1 (agreement), 2 (priced edge) and 3 (timing) live here."""

    # -- condition 1: gate agreement ---------------------------------------- #
    mode: str = "unanimous"            # "unanimous" | "weighted"
    min_conviction: float = 0.60
    min_directional_gates: int = 2
    allow_dissent: bool = False        # a directional gate pointing the other way
    gate_weights: dict[str, float] = field(default_factory=dict)

    # -- condition 2: priced edge ------------------------------------------ #
    payout_mode: str = "fixed_odds"    # "fixed_odds" | "contract_price"
    net_payout: float = 0.90           # a win pays 0.90 x stake in profit
    contract_price: float = 0.52       # prediction-market price for a $1 payout
    # Basis points of the contract's 1.00 face value, not of the price paid:
    # 175 bps is 1.75 cents per contract. VenueRules.fee_per_share matches.
    fee_bps: float = 0.0
    required_edge: float = 0.03        # p_model must clear break-even by this
    prob_cap: float = 0.64             # conviction 1.0 maps to this probability
    prob_curve: float = 1.0            # >1 makes the map more conservative

    # -- condition 3: timing ----------------------------------------------- #
    horizon_bars: int = 1              # 1 bar = 5 minutes
    bar_seconds: int = 300
    latency_seconds: int = 5            # assumed decide-to-placed delay
    max_signal_age_seconds: int = 45
    min_seconds_to_expiry: int = 60
    tie_policy: str = "loss"           # "loss" | "void" | "favor_up" | "favor_down"
    deadband_bps: float = 0.0          # |move| under this counts as a tie


# --------------------------------------------------------------------------- #
# the three risk-management conditions
# --------------------------------------------------------------------------- #


@dataclass
class RiskConfig:
    """Condition 1 (stake), 2 (loss limits) and 3 (health) live here."""

    starting_bankroll: float = 10_000.0

    # -- condition 1: stake sizing ----------------------------------------- #
    kelly_fraction: float = 0.25       # fraction of full Kelly actually used
    max_stake_pct: float = 0.02        # hard ceiling per bet, fraction of bankroll
    min_stake: float = 1.0
    stake_rounding: float = 1.0

    # -- condition 2: loss limits and exposure ----------------------------- #
    daily_loss_limit_pct: float = 0.05
    max_drawdown_pct: float = 0.20     # peak-to-trough -> full stop
    max_consecutive_losses: int = 3
    cooldown_bars: int = 6
    max_bets_per_hour: int = 4
    max_concurrent_bets: int = 1

    # -- condition 3: strategy health -------------------------------------- #
    health_window: int = 30            # bets in the rolling sample
    health_min_samples: int = 15
    hit_rate_buffer: float = 0.04      # allowed shortfall below break-even
    unhealthy_stake_derate: float = 0.5
    halt_when_unhealthy: bool = False
    atr_shock_rank: float = 0.985      # volatility spike -> stand down


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #

# Three gates, one per genuinely distinct question about a yes/no bet on the
# next five minutes:
#
#   data_integrity   is the input real?      A gapped or stuck feed turns the
#                                            bet into a coin flip at worse odds.
#   trend_alignment  which way?              The only gate whose removal drops
#                                            the stack below break-even.
#   persistence      does "which way" mean   Five-minute BTC returns are mildly
#                    anything right now?     negatively autocorrelated, so
#                                            following a trend only pays in the
#                                            subset of time when moves extend.
#
# Chosen by purpose and then confirmed on 1,042 days never used for selection,
# where it led every other three-gate combination on expected value per day.
# Three gates the stack does NOT use, and why, are in README "Cherry-picking
# three gates": volatility_regime assumes a spread this bet never crosses,
# participation adds no information once the others agree, and momentum_thrust
# is anti-predictive over the very next bar.
DEFAULT_GATE_STACK = [
    "data_integrity",
    "trend_alignment",
    "persistence",
]


@dataclass
class StrategyConfig:
    name: str = "btc-5m-binary-default"
    symbol: str = "BTCUSDT"
    # Which venue's rules apply: fee model, tie handling, minimum order, gas.
    # Declared here so a Polymarket config cannot silently be priced with
    # another venue's fee schedule. Must name a key of btc5m.venue.VENUES.
    venue: str = "predict-fun-btc-5m"
    gate_stack: list[str] = field(default_factory=lambda: list(DEFAULT_GATE_STACK))
    gates: GateParams = field(default_factory=GateParams)
    betting: BettingConfig = field(default_factory=BettingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # ------------------------------------------------------------------ #

    def validate(self) -> None:
        from .gates import GATE_REGISTRY
        from .venue import VENUES

        if self.venue not in VENUES:
            raise ValueError(
                f"unknown venue {self.venue!r}; available: {sorted(VENUES)}")

        if not self.gate_stack:
            raise ValueError("gate_stack is empty: the strategy would never bet")
        unknown = [g for g in self.gate_stack if g not in GATE_REGISTRY]
        if unknown:
            raise ValueError(
                f"unknown gate(s) {unknown}; available: {sorted(GATE_REGISTRY)}"
            )
        if len(set(self.gate_stack)) != len(self.gate_stack):
            raise ValueError("gate_stack contains duplicates")
        if not 3 <= len(self.gate_stack) <= 7:
            raise ValueError(
                f"gate_stack has {len(self.gate_stack)} gates; the design targets "
                "3-5 (7 is the hard ceiling before the stack never fires)"
            )
        if self.betting.mode not in ("unanimous", "weighted"):
            raise ValueError(f"unknown betting mode {self.betting.mode!r}")
        if self.betting.payout_mode not in ("fixed_odds", "contract_price"):
            raise ValueError(f"unknown payout mode {self.betting.payout_mode!r}")
        if self.betting.tie_policy not in ("loss", "void", "favor_up", "favor_down"):
            raise ValueError(f"unknown tie policy {self.betting.tie_policy!r}")
        if not 0.0 < self.betting.prob_cap < 1.0:
            raise ValueError("prob_cap must be strictly between 0 and 1")
        if self.betting.horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        if not 0.0 < self.risk.max_stake_pct <= 1.0:
            raise ValueError("max_stake_pct must be in (0, 1]")
        if self.risk.kelly_fraction <= 0.0:
            raise ValueError("kelly_fraction must be positive")
        if self.risk.starting_bankroll <= 0.0:
            raise ValueError("starting_bankroll must be positive")

        # A stake cap that sits at or under the minimum stake is a silent
        # lock-up, not a tight setting: the first loss drops the cap below the
        # minimum and every later bet is refused for a reason that reads like an
        # ordinary risk decision. Catch it here instead.
        ceiling = self.risk.starting_bankroll * self.risk.max_stake_pct
        if ceiling < self.risk.min_stake:
            raise ValueError(
                f"max_stake_pct {self.risk.max_stake_pct:.4%} of a "
                f"{self.risk.starting_bankroll:,.2f} bankroll is "
                f"{ceiling:,.2f}, below min_stake {self.risk.min_stake:,.2f}: "
                f"no bet could ever be placed. Raise the bankroll to at least "
                f"{self.risk.min_stake / self.risk.max_stake_pct:,.0f}, raise "
                f"max_stake_pct, or lower min_stake."
            )
        if ceiling < self.risk.min_stake * 1.5:
            raise ValueError(
                f"max_stake_pct {self.risk.max_stake_pct:.4%} of a "
                f"{self.risk.starting_bankroll:,.2f} bankroll is "
                f"{ceiling:,.2f}, only {ceiling / self.risk.min_stake:.2f}x "
                f"min_stake {self.risk.min_stake:,.2f}. A single loss drops the "
                f"cap under the minimum and the strategy stops for good. Give it "
                f"at least 1.5x headroom: bankroll "
                f"{1.5 * self.risk.min_stake / self.risk.max_stake_pct:,.0f}+."
            )

        if self.implied_conviction_floor() is None:
            raise ValueError(
                f"no conviction can clear this payout: prob_cap "
                f"{self.betting.prob_cap} caps p_model below break-even "
                f"{self.break_even_probability():.4f} + required_edge "
                f"{self.betting.required_edge}. The strategy could never bet."
            )

        directional = [g for g in self.gate_stack if GATE_REGISTRY[g].is_directional]
        if not directional:
            raise ValueError(
                "gate_stack has no directional gate; veto and confirmation gates "
                "can only refuse a side, never pick one"
            )
        if self.betting.min_directional_gates > len(directional):
            raise ValueError(
                f"min_directional_gates={self.betting.min_directional_gates} exceeds "
                f"the {len(directional)} directional gate(s) in the stack"
            )

    def break_even_probability(self) -> float:
        """Win rate at which the bet is a coin flip after payout and fees."""
        b = self.betting
        fee = b.fee_bps / 10_000.0
        if b.payout_mode == "fixed_odds":
            net = b.net_payout - fee
            if net <= 0.0:
                raise ValueError("net_payout must exceed fees")
            return (1.0 + fee) / (1.0 + net + fee)
        price = b.contract_price + fee
        if not 0.0 < price < 1.0:
            raise ValueError("contract_price plus fees must be in (0, 1)")
        return price

    def implied_conviction_floor(self) -> float | None:
        """Lowest conviction at which the priced-edge condition passes.

        The probability map is monotone, so ``required_edge`` is really a
        conviction threshold wearing different units.  Comparing this number
        against ``min_conviction`` says which of the two conditions is actually
        binding -- and whether one of them is dead code.  Returns None when no
        conviction can clear the payout, meaning the strategy can never bet.
        """
        b = self.betting
        span = b.prob_cap - 0.5
        if span <= 0.0:
            return None
        needed = (self.break_even_probability() + b.required_edge - 0.5) / span
        if needed <= 0.0:
            return 0.0
        if needed > 1.0:
            return None
        return float(needed ** (1.0 / max(1e-9, b.prob_curve)))

    def effective_conviction_floor(self) -> float:
        """The conviction a bar really has to reach, from both conditions.

        Betting conditions 1 and 2 are two thresholds on the same scalar, so the
        strategy only ever has one: the higher of the pair.  They are both kept
        because which one binds depends on the payout -- the conviction floor
        holds at generous odds, the edge requirement takes over as odds worsen --
        but nobody should read them as two independent safeguards.
        """
        implied = self.implied_conviction_floor()
        if implied is None:
            return float("inf")
        return max(self.betting.min_conviction, implied)

    def binding_betting_condition(self) -> str:
        """Which of condition 1 and condition 2 is the tighter of the two."""
        implied = self.implied_conviction_floor()
        if implied is None:
            return "priced_edge (unreachable: no conviction clears the payout)"
        if self.betting.min_conviction >= implied:
            return "min_conviction"
        return "required_edge"

    def payoff_odds(self) -> float:
        """Profit per unit staked on a win, net of fees."""
        b = self.betting
        fee = b.fee_bps / 10_000.0
        if b.payout_mode == "fixed_odds":
            return b.net_payout - fee
        price = b.contract_price + fee
        return (1.0 - price) / price


# --------------------------------------------------------------------------- #
# loading / merging
# --------------------------------------------------------------------------- #


def _merge(node: Any, overrides: dict, path: str = "") -> Any:
    """Recursively apply a dict of overrides onto a dataclass instance."""
    if not is_dataclass(node):
        return overrides
    known = {f.name: f for f in fields(node)}
    for key, value in overrides.items():
        where = f"{path}{key}"
        # Keys starting with an underscore are notes for whoever reads the file.
        if key.startswith("_"):
            continue
        if key not in known:
            raise ValueError(f"unknown config key {where!r}")
        current = getattr(node, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, path=f"{where}.")
        else:
            setattr(node, key, value)
    return node


def directional_gate_count(gate_stack: Sequence[str]) -> int:
    """How many gates in this stack can actually propose a side."""
    from .gates import GATE_REGISTRY

    return sum(1 for g in gate_stack
               if g in GATE_REGISTRY and GATE_REGISTRY[g].is_directional)


def config_from_dict(overrides: dict | None = None,
                     fit_stack: bool = False) -> StrategyConfig:
    """Build a config from defaults plus overrides.

    ``fit_stack`` lowers ``min_directional_gates`` to what the chosen stack can
    actually satisfy.  Use it when the stack comes from a preset or the command
    line: a three-gate stack carries one directional gate, and refusing to build
    it because the default asks for two is a worse answer than fitting it.  A
    hand-written config file gets the strict check instead, so a typo there is
    still an error.
    """
    cfg = StrategyConfig()
    if overrides:
        _merge(cfg, overrides)
    if fit_stack:
        available = directional_gate_count(cfg.gate_stack)
        if available:
            cfg.betting.min_directional_gates = max(
                1, min(cfg.betting.min_directional_gates, available))
    cfg.validate()
    return cfg


def load_config(path: str | Path | None = None) -> StrategyConfig:
    """Load defaults, then merge a JSON or YAML override file over them."""
    if path is None:
        return config_from_dict()
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "PyYAML is required for .yaml configs; use a .json config instead"
            ) from exc
        overrides = yaml.safe_load(text) or {}
    else:
        overrides = json.loads(text) if text.strip() else {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{p} must contain a mapping at the top level")
    return config_from_dict(overrides)


def to_dict(node: Any) -> Any:
    """Plain-data view of a config, for logging and run manifests."""
    if is_dataclass(node):
        return {f.name: to_dict(getattr(node, f.name)) for f in fields(node)}
    if isinstance(node, (list, tuple)):
        return [to_dict(v) for v in node]
    if isinstance(node, dict):
        return {k: to_dict(v) for k, v in node.items()}
    return node
