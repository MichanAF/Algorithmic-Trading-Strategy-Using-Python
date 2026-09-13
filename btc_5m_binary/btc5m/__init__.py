"""A gated, 5-minute binary up/down strategy for BTC.

The question is narrow on purpose: over the next five minutes, is BTC up or
down -- yes or no.  There is no exit, no stop and no partial fill, so the whole
strategy reduces to two decisions: whether to bet, and how much.

    gates        -> is this bar worth an opinion, and which way?
    betting      -> is that opinion worth money at the offered payout?
    risk         -> how much, and should we be betting at all right now?

Two questions the module answers about itself: ``attribution`` measures what each
gate and stack is worth on your data, and ``redundancy`` measures whether they
overlap -- a stack is only as selective as the number of independent questions it
asks.

Start with ``btc5m.quickstart`` or the command line: ``python -m btc5m --help``.
"""

from __future__ import annotations

from .attribution import (EdgeStats, compare_stacks, gate_edge, render_table,
                          stack_edge)
from .backtest import BacktestResult, run_backtest
from .config import (DEFAULT_GATE_STACK, BettingConfig, RiskConfig,
                     StrategyConfig, config_from_dict, load_config)
from .data import BarSeries, fetch_klines, load_csv, synthetic
from .features import FeatureSet, build_features
from .gates import GATE_REGISTRY, Check, Gate, GateResult, build_stack
from .redundancy import (collect_verdicts, condition_bindings, full_report,
                         leave_one_out, marginal_value, structural_overlap,
                         verdict_overlap)
from .risk import RiskDecision, RiskManager, SettledBet
from .signal import Signal, SignalEngine
from .venue import (TRUST_WALLET_BTC_5M, VENUES, MarketDecision, MarketQuote,
                    VenueRules, evaluate_market, minimum_viable_stake)

__version__ = "1.0.0"

__all__ = [
    "BarSeries", "load_csv", "fetch_klines", "synthetic",
    "StrategyConfig", "BettingConfig", "RiskConfig", "config_from_dict",
    "load_config", "DEFAULT_GATE_STACK",
    "FeatureSet", "build_features",
    "Gate", "GateResult", "Check", "GATE_REGISTRY", "build_stack",
    "Signal", "SignalEngine",
    "RiskManager", "RiskDecision", "SettledBet",
    "run_backtest", "BacktestResult",
    "gate_edge", "stack_edge", "compare_stacks", "render_table", "EdgeStats",
    "structural_overlap", "verdict_overlap", "marginal_value", "leave_one_out",
    "condition_bindings", "collect_verdicts", "full_report",
    "VenueRules", "MarketQuote", "MarketDecision", "evaluate_market",
    "minimum_viable_stake", "VENUES", "TRUST_WALLET_BTC_5M",
    "quickstart",
]


def quickstart(bars: int = 20_000, seed: int = 7):
    """Run the default stack on synthetic bars and return the result.

    Synthetic data proves the machinery works; it says nothing about live edge.
    Point ``load_csv`` or ``fetch_klines`` at real history for that.
    """
    cfg = config_from_dict()
    return run_backtest(synthetic(bars, seed=seed), cfg)
