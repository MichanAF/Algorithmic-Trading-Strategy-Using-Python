"""A hedged crypto book built from the instruments MetaMask actually has.

Spot you own, a short perp against it, and one number -- the hedge ratio --
that decides how much of the core's price exposure is switched off right now.

The venue's fee schedule writes the architecture:

    MetaMask Swaps      0.875% per side
    Hyperliquid perps   0.035% taker, -0.010% maker rebate

Moving exposure through the spot book costs twenty-five times what it costs
through the perp book, so the core is bought once and never traded, and every
exposure decision is an adjustment to the short. And because MetaMask Perps is
**isolated margin only**, the core's gain in a rally does not defend the short
that was hedging it -- which is why ``sizing`` derives the split from the rally
the hedge must survive, rather than from anyone's preference.

    plan     -> what goes where, and why that much
    carry    -> how long a hedge must be held to have paid for itself
    hedge    -> what ratio right now, with the reasons attached
    risk     -> the margin ladder, priced before you need it
    compare  -> overlay vs HODL vs always-neutral, on the same series

Start with ``python -m mmhedge --help``.

Nothing here is financial advice. It is a model of a venue's cost structure and
a policy defined on top of it; the numbers it prints are only as good as the
fees, funding and margin brackets you feed it, all of which the venue can
change without asking.
"""

from __future__ import annotations

from .backtest import (BacktestResult, Signals, build_signals, compare,
                       render_table, run_backtest)
from .carry import (CarryQuote, break_even_table, funding_apr_needed,
                    quote_carry, realised_funding)
from .checks import Check
from .config import (CarryParams, CashParams, CoreParams, HedgeParams,
                     RiskParams, StrategyConfig, config_from_dict, load_config,
                     to_dict)
from .data import Series, load_csv, synthetic
from .hedge import (HedgeDecision, MarketState, carry_component, decide,
                    risk_component)
from .risk import LadderRung, MarginLadder, RiskReport, assess, build_ladder
from .sizing import (Allocation, AssetSlice, allocate, leverage_for_survival,
                     liquidation_move, margin_fraction_for_survival)
from .venue import METAMASK, MarginTier, MetaMaskVenue

__version__ = "1.0.0"

__all__ = [
    "METAMASK", "MetaMaskVenue", "MarginTier",
    "StrategyConfig", "config_from_dict", "load_config", "to_dict",
    "CoreParams", "HedgeParams", "CarryParams", "RiskParams", "CashParams",
    "Check",
    "CarryQuote", "quote_carry", "funding_apr_needed", "break_even_table",
    "realised_funding",
    "Allocation", "AssetSlice", "allocate", "margin_fraction_for_survival",
    "liquidation_move", "leverage_for_survival",
    "MarketState", "HedgeDecision", "decide", "carry_component", "risk_component",
    "RiskReport", "MarginLadder", "LadderRung", "assess", "build_ladder",
    "Series", "synthetic", "load_csv",
    "BacktestResult", "Signals", "build_signals", "run_backtest", "compare",
    "render_table",
]
