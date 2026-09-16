"""Command line for the hedged MetaMask book.

    python -m mmhedge venue                          # what the venue charges
    python -m mmhedge plan --capital 25000           # the split, derived
    python -m mmhedge carry --funding 0.25           # how long to hold
    python -m mmhedge hedge --funding 0.30 --trend -0.4
    python -m mmhedge risk --capital 25000 --hedge 1.0 --price 95000
    python -m mmhedge compare --synthetic 8760       # overlay vs HODL vs neutral
    python -m mmhedge sweep --seeds 40               # is any of it robust?
"""

from __future__ import annotations

import argparse
import json
import sys

from .backtest import compare as compare_policies
from .backtest import render_table, run_backtest
from .carry import break_even_table, funding_apr_needed, quote_carry
from .config import StrategyConfig, config_from_dict, load_config, to_dict
from .data import load_csv, synthetic
from .hedge import MarketState, decide
from .risk import assess
from .sizing import allocate


def _coerce(text: str):
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _build_config(args) -> StrategyConfig:
    cfg = load_config(args.config) if getattr(args, "config", None) else config_from_dict()
    for override in getattr(args, "set", []) or []:
        if "=" not in override:
            raise SystemExit(f"--set needs KEY=VALUE, got {override!r}")
        key, _, value = override.partition("=")
        node = cfg
        parts = key.split(".")
        for part in parts[:-1]:
            if not hasattr(node, part):
                raise SystemExit(f"unknown config section {part!r} in {key!r}")
            node = getattr(node, part)
        if not hasattr(node, parts[-1]):
            raise SystemExit(f"unknown config key {key!r}")
        setattr(node, parts[-1], _coerce(value))
    if getattr(args, "capital", None):
        cfg.capital_usd = float(args.capital)
    if getattr(args, "leverage", None):
        cfg.hedge.leverage = float(args.leverage)
    cfg.normalise()
    return cfg


def _series(args):
    if getattr(args, "data", None):
        return load_csv(args.data, symbol=getattr(args, "symbol", None) or "BTC")
    hours = getattr(args, "synthetic", None) or 24 * 365
    return synthetic(hours, seed=getattr(args, "seed", 7))


def _add_config_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("strategy")
    g.add_argument("--config", metavar="JSON|YAML", help="config override file")
    g.add_argument("--set", metavar="KEY=VALUE", action="append", default=[],
                   help="override one value, e.g. risk.survive_rally_pct=0.35")
    g.add_argument("--capital", type=float, help="capital in USD")
    g.add_argument("--leverage", type=float, help="hedge leverage override")


def _add_data_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("data (pick one)")
    g.add_argument("--data", metavar="CSV",
                   help="hourly timestamp,price,funding CSV")
    g.add_argument("--symbol", default="BTC")
    g.add_argument("--synthetic", metavar="HOURS", type=int,
                   help="generate hours of correlated price and funding")
    g.add_argument("--seed", type=int, default=7)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_venue(args) -> int:
    cfg = _build_config(args)
    print(cfg.venue.summary())
    print()
    print("what this means for the strategy:")
    print(f"  moving $10,000 of exposure with a swap costs "
          f"${10_000 * cfg.venue.spot_cost_pct():,.2f}")
    print(f"  moving $10,000 of exposure with the perp costs "
          f"${10_000 * cfg.venue.perp_taker_pct:,.2f} taker, "
          f"${10_000 * cfg.venue.perp_maker_pct:,.2f} maker")
    print("  so: buy the core once, and manage every exposure decision in the perp.")
    return 0


def cmd_plan(args) -> int:
    cfg = _build_config(args)
    alloc = allocate(cfg, cash_floor_pct=args.cash_floor)
    print(alloc.report())
    print()
    y = alloc.expected_annual_yield(cfg.venue, args.funding, args.hedge,
                                    cfg.cash.staking_gross_apr)
    print(f"expected carry at {args.funding:.0%} funding, h={args.hedge:.2f} "
          f"(price return excluded on purpose):")
    for k in ("funding", "musd_cash", "eth_staking"):
        print(f"  {k:<14} {y[k]:>10,.0f} USD/yr")
    print(f"  {'total':<14} {y['total']:>10,.0f} USD/yr "
          f"({y['on_capital']:.2%} on capital)")
    return 0


def cmd_carry(args) -> int:
    cfg = _build_config(args)
    print(break_even_table(cfg.venue, leverage=cfg.hedge.leverage))
    print()
    for from_cash in (False, True):
        q = quote_carry(cfg.venue, args.funding, leverage=cfg.hedge.leverage,
                        from_cash=from_cash, maker=not args.taker,
                        notional_usd=args.notional, withdraw=args.notional > 0,
                        safety_multiple=cfg.carry.hold_safety_multiple,
                        floor_days=cfg.carry.min_hold_days)
        print(q.report())
        print()
    for d in (3, 7, 30):
        print(f"a {d}-day hold needs "
              f"{funding_apr_needed(cfg.venue, d, leverage=cfg.hedge.leverage, maker=not args.taker):.2%} "
              f"funding as an overlay, "
              f"{funding_apr_needed(cfg.venue, d, leverage=cfg.hedge.leverage, from_cash=True, maker=not args.taker):.2%} "
              f"built from cash")
    return 0


def cmd_hedge(args) -> int:
    cfg = _build_config(args)
    state = MarketState(
        funding_apr=args.funding, funding_now_apr=args.funding,
        negative_funding_hours=args.negative_hours, trend_score=args.trend,
        vol_rank=args.vol_rank, core_drawdown=args.drawdown,
        current_ratio=args.current, days_held=args.days_held,
        core_usd=args.core or (cfg.capital_usd * 0.6),
        cooldown_active=args.cooldown)
    print(decide(state, cfg).report())
    return 0


def cmd_risk(args) -> int:
    cfg = _build_config(args)
    alloc = allocate(cfg, cash_floor_pct=args.cash_floor)
    print(assess(cfg, alloc, hedge_ratio=args.hedge,
                 adverse_move_so_far=args.moved, funding_apr=args.funding,
                 negative_funding_hours=args.negative_hours,
                 core_drawdown=args.drawdown, entry_price=args.price).report())
    return 0


def cmd_backtest(args) -> int:
    cfg = _build_config(args)
    series = _series(args)
    print(series.summary())
    print()
    result = run_backtest(series, cfg, policy=args.policy,
                          decision_hours=args.decision_hours,
                          cash_floor_pct=args.cash_floor)
    print(result.summary())
    if result.events and args.events:
        print("\nevents:")
        for e in result.events[:args.events]:
            print(f"  {e}")
        if len(result.events) > args.events:
            print(f"  ... {len(result.events) - args.events} more")
    return 0


def cmd_compare(args) -> int:
    cfg = _build_config(args)
    series = _series(args)
    print(series.summary())
    print()
    print(render_table(compare_policies(series, cfg,
                                        decision_hours=args.decision_hours,
                                        cash_floor_pct=args.cash_floor)))
    print()
    print("negative net fees are maker rebates earned, not costs.")
    return 0


def cmd_sweep(args) -> int:
    import numpy as np
    cfg = _build_config(args)
    seeds = list(range(1, args.seeds + 1))
    print(f"{args.seeds} synthetic years, funding_beta={args.beta}")
    print("(beta couples funding to price momentum; run it at 0 to see how much "
          "of any\n edge is the coupling rather than the policy)\n")
    rows: dict[str, list] = {}
    for sd in seeds:
        series = synthetic(args.hours, seed=sd, funding_beta=args.beta)
        for r in compare_policies(series, cfg, decision_hours=args.decision_hours,
                                  cash_floor_pct=args.cash_floor):
            rows.setdefault(r.label, []).append(
                (r.cagr, r.ann_vol, r.max_drawdown, r.sharpe,
                 r.deleverages, r.liquidations))
    hodl = np.array(rows["core only (HODL)"])
    head = (f"{'strategy':<22} {'med CAGR':>9} {'med vol':>8} {'med DD':>8} "
            f"{'med r/v':>8} {'delev/yr':>9} {'liq':>5} {'beat HODL':>10}")
    print(head)
    print("-" * len(head))
    for label, vals in rows.items():
        a = np.array(vals)
        beat = "        --" if label == "core only (HODL)" else \
            f"{np.mean(a[:, 0] > hodl[:, 0]):>9.0%}"
        print(f"{label:<22} {np.median(a[:, 0]):>+8.1%} {np.median(a[:, 1]):>7.1%} "
              f"{np.median(a[:, 2]):>7.1%} {np.median(a[:, 3]):>8.2f} "
              f"{a[:, 4].mean():>9.0f} {a[:, 5].sum():>5.0f} {beat}")
    return 0


def cmd_config(args) -> int:
    print(json.dumps(to_dict(_build_config(args)), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mmhedge", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("venue", help="what MetaMask and Hyperliquid charge")
    _add_config_args(v)
    v.set_defaults(func=cmd_venue)

    pl = sub.add_parser("plan", help="derive the split from the hedge policy")
    _add_config_args(pl)
    pl.add_argument("--cash-floor", type=float, default=0.10,
                    help="fraction kept in mUSD before sizing (default: 0.10)")
    pl.add_argument("--funding", type=float, default=0.15,
                    help="funding APR to price the carry at")
    pl.add_argument("--hedge", type=float, default=0.50,
                    help="hedge ratio to price the carry at")
    pl.set_defaults(func=cmd_plan)

    c = sub.add_parser("carry", help="break-even hold, and what funding it needs")
    _add_config_args(c)
    c.add_argument("--funding", type=float, default=0.20, help="funding APR")
    c.add_argument("--notional", type=float, default=0.0,
                   help="hedge notional, to amortise the $1 withdrawal")
    c.add_argument("--taker", action="store_true", help="assume taker fills")
    c.set_defaults(func=cmd_carry)

    h = sub.add_parser("hedge", help="what hedge ratio, and why")
    _add_config_args(h)
    h.add_argument("--funding", type=float, default=0.15, help="trailing funding APR")
    h.add_argument("--trend", type=float, default=0.0, help="-1 broken .. +1 intact")
    h.add_argument("--vol-rank", type=float, default=0.5)
    h.add_argument("--drawdown", type=float, default=0.0)
    h.add_argument("--current", type=float, default=0.0, help="hedge already on")
    h.add_argument("--days-held", type=float, default=0.0)
    h.add_argument("--negative-hours", type=int, default=0)
    h.add_argument("--core", type=float, help="core value in USD")
    h.add_argument("--cooldown", action="store_true",
                   help="a forced deleverage is still cooling off")
    h.set_defaults(func=cmd_hedge)

    r = sub.add_parser("risk", help="risk conditions and the margin ladder")
    _add_config_args(r)
    r.add_argument("--cash-floor", type=float, default=0.10)
    r.add_argument("--hedge", type=float, default=1.0, help="hedge ratio on")
    r.add_argument("--price", type=float, help="entry price, to print levels")
    r.add_argument("--moved", type=float, default=0.0,
                   help="adverse move already seen")
    r.add_argument("--funding", type=float, default=0.15)
    r.add_argument("--negative-hours", type=int, default=0)
    r.add_argument("--drawdown", type=float, default=0.0)
    r.set_defaults(func=cmd_risk)

    b = sub.add_parser("backtest", help="run one policy")
    _add_config_args(b)
    _add_data_args(b)
    b.add_argument("--policy", choices=("adaptive", "neutral", "none"),
                   default="adaptive")
    b.add_argument("--decision-hours", type=int, default=24)
    b.add_argument("--cash-floor", type=float, default=0.10)
    b.add_argument("--events", type=int, default=10, help="events to print")
    b.set_defaults(func=cmd_backtest)

    cp = sub.add_parser("compare", help="overlay vs HODL vs always-neutral")
    _add_config_args(cp)
    _add_data_args(cp)
    cp.add_argument("--decision-hours", type=int, default=24)
    cp.add_argument("--cash-floor", type=float, default=0.10)
    cp.set_defaults(func=cmd_compare)

    sw = sub.add_parser("sweep", help="many synthetic years: is it robust?")
    _add_config_args(sw)
    sw.add_argument("--seeds", type=int, default=40)
    sw.add_argument("--hours", type=int, default=24 * 365)
    sw.add_argument("--beta", type=float, default=0.55,
                    help="funding/momentum coupling (0 removes it)")
    sw.add_argument("--decision-hours", type=int, default=24)
    sw.add_argument("--cash-floor", type=float, default=0.10)
    sw.set_defaults(func=cmd_sweep)

    cf = sub.add_parser("config", help="print the resolved config")
    _add_config_args(cf)
    cf.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
