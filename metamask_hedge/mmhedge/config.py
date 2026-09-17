"""Typed configuration for the hedged MetaMask book.

Same contract as the 5-minute strategy next door: every threshold lives here,
so a tuning run is a config diff and never a code diff.  JSON and YAML both
load, merged over the defaults, so an override file carries only the keys it
changes.

The defaults are deliberately the conservative preset.  A hedge sized to
survive a +50% rally unattended is barely levered, and that is the correct
place to start from when the failure mode is a liquidation that leaves you
unhedged at the top of a squeeze.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .venue import VENUES, Venue


@dataclass
class CoreParams:
    """The spot you actually want to own, and intend to keep owning.

    ``weights`` are fractions of the core sleeve, normalised on load.  The
    assets here are the ones MetaMask holds natively: BTC (native since
    December 2025), ETH, SOL, and EVM tokens.

    ``staked_pct`` is the share of a holding sitting in MetaMask's pooled
    staking.  It still carries full price delta -- the perp hedges it fine --
    but it is not liquid on a bad day and it is never perp collateral, so
    ``sizing`` reports it separately.  MetaMask keeps 15% of the rewards.
    """

    weights: dict[str, float] = field(
        default_factory=lambda: {"BTC": 0.55, "ETH": 0.30, "SOL": 0.15})
    staked_pct: dict[str, float] = field(default_factory=lambda: {"ETH": 0.50})
    # Drift tolerated before the core is worth touching at all.  Wide on
    # purpose: at 1.95% a round trip, a 5% band rebalanced monthly burns more
    # than it corrects.
    rebalance_band: float = 0.15
    # "perp" | "swap" | "flows".  The default is the whole thesis: correct
    # drift with the overlay, which costs 0.07%, not the book, which costs 1.95%.
    rebalance_with: str = "perp"
    min_swap_notional_usd: float = 2_000.0


@dataclass
class HedgeParams:
    """The overlay: a short perp whose size is the only thing that moves."""

    max_hedge_ratio: float = 1.00      # 1.0 = fully delta neutral
    min_hedge_ratio: float = 0.00      # 0.0 = fully exposed core
    leverage: float = 2.0              # survives +48.5% on a major
    # Never hedge the whole book in one click.  Funding mean-reverts and
    # regime calls are wrong often enough that an averaged entry is worth more
    # than a precise one.
    ladder_steps: int = 3
    min_step: float = 0.10             # smaller moves are not worth the fee
    # How the carry signal and the risk signal combine.  "max" means either one
    # can call for a hedge on its own, which is the honest reading: they are
    # different reasons, not two votes on the same question.
    combine: str = "max"
    carry_weight: float = 1.0
    risk_weight: float = 1.0
    use_maker_orders: bool = True
    hedge_symbol_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class CarryParams:
    """When funding is rich enough that hedging pays for itself."""

    # Annualised funding, trailing, above which the carry signal starts ramping.
    # Baseline funding is ~11% APR, so 15% means "meaningfully above the
    # interest component", not "positive".
    enter_apr: float = 0.15
    full_apr: float = 0.45             # carry component reaches 1.0 here
    exit_apr: float = 0.05
    lookback_hours: int = 24
    # A carry hedge held below its break-even is a fee donation.  The guard
    # refuses to close one early unless funding flipped or risk took over.
    hold_safety_multiple: float = 3.0
    min_hold_days: float = 2.0
    max_hold_days: float = 120.0
    # Funding negative this many hours running: the trade's premise is gone.
    flip_exit_hours: int = 8


@dataclass
class RiskParams:
    """What the hedge has to survive, and when it gets topped up."""

    # The rally the short must survive with no human present.  This is the
    # number that sets leverage, and through leverage sets the whole split.
    survive_rally_pct: float = 0.50
    # Top up when this fraction of the distance to liquidation is gone.
    margin_call_at: float = 0.50
    # Second, louder ladder rung.
    margin_urgent_at: float = 0.70
    # Reserve held as USDC on Arbitrum -- one transaction from the perp
    # account.  mUSD on Monad is the wrong place for it: the yield is 4% and
    # the bridge is the problem.
    reserve_multiple: float = 1.0
    reserve_on_arbitrum: bool = True
    # When the reserve and idle cash are both exhausted and the short is still
    # under-collateralised, there are three ways out and only one of them is
    # cheap.  Selling core to fund margin costs 0.875% and keeps the hedge;
    # trimming the short costs 0.035% and fixes the margin ratio directly;
    # doing nothing costs the entire posted collateral.  The venue's own fee
    # schedule picks the winner: move the leg that is cheap to move.
    # "deleverage" | "none"
    margin_last_resort: str = "deleverage"
    # Trim once collateral falls to this multiple of the bare maintenance
    # requirement -- above 1.0 so the cut happens before the venue's own
    # liquidation engine gets there, not at the same instant.
    deleverage_trigger: float = 2.0
    # The survival distance the trimmed position is rebuilt to.  Smaller than
    # the opening ceiling on purpose: the point is to keep some hedge alive
    # through the squeeze, not to re-establish the original plan at the highs.
    deleverage_target_survival: float = 0.25
    # After a forced trim, refuse to rebuild the hedge for this long.  Without
    # it the policy re-shorts into the same squeeze that just trimmed it, gets
    # trimmed again, and pays taker fees on every lap -- a doom loop that
    # testing turned up at roughly 130 forced trims a year in a strong rally.
    # The squeeze is the market saying this hedge is wrong right now; arguing
    # with it daily is expensive.
    deleverage_cooldown_hours: int = 72
    max_core_drawdown_pct: float = 0.35
    max_single_asset_weight: float = 0.60
    min_core_liquid_pct: float = 0.40
    # Volatility rank above which the risk signal alone will call for a hedge.
    vol_shock_rank: float = 0.85
    trend_break_weight: float = 0.60
    drawdown_weight: float = 0.40


@dataclass
class CashParams:
    """Where the un-deployed dollars sit."""

    musd_apy: float = 0.04
    idle_to_musd: bool = True
    # Gross ETH staking APR before MetaMask's 15% cut.  Used only to report
    # what the staked sleeve contributes.
    staking_gross_apr: float = 0.03


@dataclass
class StrategyConfig:
    name: str = "metamask-hedged-core"
    capital_usd: float = 10_000.0
    venue: Venue = field(default_factory=Venue)
    core: CoreParams = field(default_factory=CoreParams)
    hedge: HedgeParams = field(default_factory=HedgeParams)
    carry: CarryParams = field(default_factory=CarryParams)
    risk: RiskParams = field(default_factory=RiskParams)
    cash: CashParams = field(default_factory=CashParams)

    def __post_init__(self) -> None:
        self.normalise()

    def normalise(self) -> None:
        total = sum(self.core.weights.values())
        if total <= 0.0:
            raise ValueError("core.weights must sum to something positive")
        self.core.weights = {k.strip().upper(): v / total
                            for k, v in self.core.weights.items()}
        self.core.staked_pct = {k.strip().upper(): v
                                for k, v in self.core.staked_pct.items()}
        self.validate()

    def validate(self) -> None:
        h, r, c = self.hedge, self.risk, self.carry
        if not 0.0 <= h.min_hedge_ratio <= h.max_hedge_ratio <= 2.0:
            raise ValueError("need 0 <= min_hedge_ratio <= max_hedge_ratio <= 2")
        if h.leverage <= 0.0:
            raise ValueError("hedge.leverage must be positive")
        if h.ladder_steps < 1:
            raise ValueError("hedge.ladder_steps must be >= 1")
        if h.combine not in ("max", "sum", "mean"):
            raise ValueError("hedge.combine must be max, sum or mean")
        if self.core.rebalance_with not in ("perp", "swap", "flows"):
            raise ValueError("core.rebalance_with must be perp, swap or flows")
        if not 0.0 < r.survive_rally_pct < 5.0:
            raise ValueError("risk.survive_rally_pct must be in (0, 5)")
        if not 0.0 < r.margin_call_at < r.margin_urgent_at < 1.0:
            raise ValueError("need 0 < margin_call_at < margin_urgent_at < 1")
        if r.margin_last_resort not in ("deleverage", "none"):
            raise ValueError("risk.margin_last_resort must be deleverage or none")
        if c.exit_apr > c.enter_apr:
            raise ValueError("carry.exit_apr above enter_apr would thrash the hedge")
        if c.full_apr <= c.enter_apr:
            raise ValueError("carry.full_apr must exceed carry.enter_apr")

        # The one cross-check worth failing loudly on: leverage the venue will
        # not grant, on the least forgiving asset in the core.
        for symbol in self.core.weights:
            tier = self.venue.tier_for(self.hedge_symbol(symbol))
            if h.leverage > tier.max_leverage:
                raise ValueError(
                    f"hedge.leverage {h.leverage} exceeds the {tier.max_leverage}x "
                    f"ceiling for {symbol} ({tier.name} tier)")

    def hedge_symbol(self, core_symbol: str) -> str:
        """Which perp market hedges a given spot holding.

        Usually itself.  The override exists for holdings with no perp of their
        own, or ones whose perp is too illiquid to short in size -- you hedge
        those with a correlated major and accept the basis, which
        ``risk.py`` will tell you about.
        """
        s = core_symbol.strip().upper()
        return self.hedge.hedge_symbol_overrides.get(s, s).strip().upper()

    def hedged_symbols(self) -> dict[str, str]:
        return {s: self.hedge_symbol(s) for s in self.core.weights}

    def blended_maintenance_margin(self) -> float:
        """Core-weighted maintenance margin across the perps that hedge it."""
        return sum(w * self.venue.tier_for(self.hedge_symbol(s)).maintenance_margin
                   for s, w in self.core.weights.items())


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _merge(target: Any, patch: dict) -> Any:
    for key, value in patch.items():
        # JSON has no comments, so an underscore prefix is the convention for
        # notes that live with the config -- `_warning`, `_source`, `_checked`.
        # They are skipped rather than rejected, because a preset that carries
        # its own caveat is worth more than one that cannot.
        if key.startswith("_"):
            continue
        if not hasattr(target, key):
            raise ValueError(f"unknown config key {key!r} for {type(target).__name__}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(target, key, value)
    return target


def _venue_preset(name: str) -> Venue:
    key = str(name).strip().lower()
    if key not in VENUES:
        raise ValueError(
            f"unknown venue {name!r}; have {sorted(VENUES)}")
    return VENUES[key]


def config_from_dict(data: dict | None = None) -> StrategyConfig:
    cfg = StrategyConfig()
    if data:
        payload = dict(data)
        venue_patch = payload.pop("venue", None)
        if isinstance(venue_patch, str):
            # "venue": "okx" -- pick a preset wholesale.
            cfg.venue = _venue_preset(venue_patch)
        elif venue_patch:
            patch = dict(venue_patch)
            preset = patch.pop("preset", None)
            base = _venue_preset(preset) if preset else cfg.venue
            cfg.venue = base.with_overrides(**patch) if patch else base
        _merge(cfg, payload)
    cfg.normalise()
    return cfg


def load_config(path: str | Path) -> StrategyConfig:
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml  # optional dependency; JSON needs nothing
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return config_from_dict(data or {})


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj
