"""Command line for the 5-minute BTC binary strategy.

    python -m btc5m signal   --synthetic 5000        # full gate trace, latest bar
    python -m btc5m backtest --data btc_5m.csv       # walk-forward run
    python -m btc5m gates    --data btc_5m.csv       # what is each gate worth?
    python -m btc5m compare  --data btc_5m.csv       # which stack should I run?
    python -m btc5m overlap  --data btc_5m.csv       # are the gates independent?
    python -m btc5m flow     --data btc_5m.csv --minute btc_1m.csv   # does pre-open flow predict?
    python -m btc5m quote    --data btc_5m.csv --down 51   # price a live market
    python -m btc5m probe    --testnet                     # see predict.fun's real payload
    python -m btc5m probe    --venue polymarket-btc-5m    # see Polymarket's real payload
    python -m btc5m watch    --windows 12 --out quotes.csv  # log the live quote, no orders
    python -m btc5m settle   --quotes quotes.csv           # fill in who won
    python -m btc5m report   --quotes quotes.csv           # was the market ever near even?
    python -m btc5m settlement --minute y-1m.csv --data y.csv  # is it even the same bet?
    python -m btc5m live     --data btc_5m.csv --testnet   # price the live window
    python -m btc5m fetch    --exchange binance -o btc_5m.csv
    python -m btc5m fetch    --interval 1m --year -o btc_1m.csv      # minute bars for --minute
    python -m btc5m menu                             # the gate menu
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .attribution import (compare_stacks, flow_correlation, flow_edge, gate_edge,
                          render_flow, render_table)
from .redundancy import full_report
from .backtest import run_backtest
from .config import (DEFAULT_GATE_STACK, StrategyConfig, config_from_dict,
                     load_config, to_dict)
from .data import (INTERVAL_SECONDS, BarSeries, fetch_binance_dump,
                   fetch_history, fetch_klines, load_csv, synthetic)
from .venue import (POLYMARKET_BTC_5M, VENUES, MarketQuote, evaluate_market,
                    minimum_viable_stake)
from .predictfun import PredictFunClient, PredictFunError, probe as probe_predictfun
from .polymarket import PolymarketClient, probe as probe_polymarket
from .settlement import (compare as compare_settlement, hit_rates,
                         render_comparison, render_hit_rates,
                         render_venue_scores, score_against_venue,
                         window_prices)
from .watch import (DEFAULT_OFFSETS, Watcher, read_rows, render_report,
                    settle as settle_quotes)
from .features import build_features
from .gates import GATE_REGISTRY
from .signal import SignalEngine

PRESETS: dict[str, list[str]] = {
    "default": list(DEFAULT_GATE_STACK),
    "core3": ["data_integrity", "volatility_regime", "trend_alignment"],
    "trend5": ["data_integrity", "volatility_regime", "trend_alignment",
               "persistence", "participation"],
    # Best risk-adjusted result on the bundled fixture: participation's verdict
    # added no information once the other gates agreed, and dropping it raised
    # Sharpe from 3.6 to 6.2 by trading five times as often.
    "trend4": ["data_integrity", "volatility_regime", "trend_alignment",
               "persistence"],
    "trend6-session": ["data_integrity", "volatility_regime", "session",
                       "trend_alignment", "persistence", "participation"],
    "thrust": ["data_integrity", "volatility_regime", "trend_alignment",
               "momentum_thrust", "participation"],
    # Unproven: offered so you can measure it, not because it tested well.
    # `persistence` is deliberately absent -- it contradicts `mean_reversion`.
    "meanrev": ["data_integrity", "volatility_regime", "mean_reversion"],
    "everything": ["data_integrity", "volatility_regime", "trend_alignment",
                   "persistence", "participation", "momentum_thrust", "location"],
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _add_data_args(p: argparse.ArgumentParser, reference: bool = True) -> None:
    src = p.add_argument_group("data source (pick one)")
    src.add_argument("--data", metavar="CSV", help="5-minute OHLCV CSV file")
    src.add_argument("--exchange",
                     choices=("binance", "binance-vision", "coinbase", "kraken"),
                     help="fetch recent closed 5m candles live "
                          "(binance-vision is Binance's public mirror, which "
                          "answers where api.binance.com is geo-blocked)")
    src.add_argument("--symbol", help="exchange symbol override")
    src.add_argument("--limit", type=int, default=1000,
                     help="candles to fetch (default: 1000)")
    src.add_argument("--synthetic", metavar="BARS", type=int,
                     help="generate seeded synthetic bars instead")
    src.add_argument("--seed", type=int, default=7, help="synthetic seed")
    if reference:
        src.add_argument("--reference", metavar="CSV",
                         help="correlated series (ETH) for the cross_asset gate")
    src.add_argument("--minute", metavar="CSV",
                     help="1-minute bars of the same symbol, read when "
                          "gates.taker_flow.source is 1m (fetch --interval 1m)")


def _add_config_args(p: argparse.ArgumentParser) -> None:
    grp = p.add_argument_group("strategy")
    grp.add_argument("--config", metavar="JSON|YAML", help="config override file")
    grp.add_argument("--preset", choices=sorted(PRESETS),
                     help="gate stack preset (default: the built-in default)")
    grp.add_argument("--gates", metavar="A,B,C",
                     help="explicit comma-separated gate stack")
    grp.add_argument("--set", metavar="KEY=VALUE", action="append", default=[],
                     help="override one config value, e.g. betting.net_payout=0.95")


def _coerce(text: str):
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    if text.strip().startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "," in text:
        return [part.strip() for part in text.split(",")]
    return text


def _build_config(args) -> StrategyConfig:
    cfg = load_config(args.config) if getattr(args, "config", None) else config_from_dict()
    overrides: dict = {}
    if getattr(args, "preset", None):
        overrides["gate_stack"] = list(PRESETS[args.preset])
    if getattr(args, "gates", None):
        overrides["gate_stack"] = [g.strip() for g in args.gates.split(",") if g.strip()]
    for item in getattr(args, "set", []) or []:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        node = overrides
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(value)
    if overrides:
        merged = to_dict(cfg)
        _deep_update(merged, overrides)
        # A stack chosen on the command line should not be refused just because
        # the default asks for more directional gates than it holds.
        stack_changed = "gate_stack" in overrides
        asked_explicitly = "min_directional_gates" in overrides.get("betting", {})
        fit = stack_changed and not asked_explicitly
        before = merged["betting"]["min_directional_gates"]
        cfg = config_from_dict(merged, fit_stack=fit)
        after = cfg.betting.min_directional_gates
        if after != before:
            print(f"note: this stack holds {after} directional gate(s), so "
                  f"min_directional_gates was lowered from {before} to {after}.",
                  file=sys.stderr)
    cfg.validate()
    return cfg


def _deep_update(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _load_series(args) -> BarSeries:
    chosen = [bool(getattr(args, "data", None)), bool(getattr(args, "exchange", None)),
              getattr(args, "synthetic", None) is not None]
    if sum(chosen) > 1:
        raise SystemExit("pick one data source: --data, --exchange or --synthetic")
    if getattr(args, "data", None):
        return load_csv(args.data)
    if getattr(args, "exchange", None):
        return fetch_klines(args.exchange, args.symbol, args.limit)
    bars = getattr(args, "synthetic", None)
    if bars is None:
        raise SystemExit(
            "no data source given. Use --data FILE.csv, --exchange binance, "
            "or --synthetic 20000 to try the machinery on generated bars.")
    if bars < 400:
        raise SystemExit(f"--synthetic {bars} is below the ~300-bar warm-up; "
                         "use at least 400")
    return synthetic(bars, seed=args.seed)


def _load_reference(args) -> BarSeries | None:
    path = getattr(args, "reference", None)
    return load_csv(path) if path else None


def _load_minute(args) -> BarSeries | None:
    path = getattr(args, "minute", None)
    if not path:
        return None
    minute = load_csv(path)
    if minute.bar_seconds != 60:
        raise SystemExit(f"--minute {path} holds {minute.bar_seconds}-second "
                         f"bars, not 1-minute ones; fetch it with --interval 1m")
    if minute.taker_buy is None:
        raise SystemExit(f"--minute {path} has no taker_buy column; only a "
                         f"binance fetch carries it")
    return minute


def _note_minute(cfg: StrategyConfig, minute: BarSeries | None) -> None:
    """A 1m-sourced taker_flow gate with no minute series never fires."""
    tf = cfg.gates.taker_flow
    if tf.source == "1m" and minute is None and "taker_flow" in cfg.gate_stack:
        print("note: gates.taker_flow.source is 1m but no --minute CSV was "
              "given, so taker_flow stays in warm-up and never votes.",
              file=sys.stderr)


def _warn_if_synthetic(args) -> None:
    if getattr(args, "synthetic", None) is not None:
        print("note: synthetic bars. These validate the machinery, not the edge.\n",
              file=sys.stderr)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_backtest(args) -> int:
    cfg = _build_config(args)
    series = _load_series(args)
    _warn_if_synthetic(args)
    minute = _load_minute(args)
    _note_minute(cfg, minute)
    result = run_backtest(series, cfg, reference=_load_reference(args),
                          minute=minute)
    print(result.summary())
    if args.json:
        Path(args.json).write_text(json.dumps({
            "config": result.config,
            "bets": len(result.bets),
            "hit_rate": result.hit_rate,
            "break_even": result.break_even,
            "net_pnl": result.net_pnl,
            "return_pct": result.return_pct,
            "max_drawdown": result.max_drawdown,
            "expectancy_per_stake": result.expectancy_per_bet,
            "sharpe": result.sharpe,
            "calibration": result.calibration(),
            "equity": result.equity,
        }, indent=2, default=float))
        print(f"\nwrote {args.json}")
    return 0


def cmd_signal(args) -> int:
    cfg = _build_config(args)
    series = _load_series(args)
    _warn_if_synthetic(args)
    minute = _load_minute(args)
    _note_minute(cfg, minute)
    fs = build_features(series, cfg, _load_reference(args), minute=minute)
    engine = SignalEngine(cfg)
    warmup = engine.warmup_bars(fs)
    index = args.bar if args.bar is not None else len(series) - 1
    if index < 0:
        index += len(series)
    if not 0 <= index < len(series):
        raise SystemExit(f"--bar {args.bar} is outside 0..{len(series) - 1}")
    if index < warmup:
        raise SystemExit(f"bar {index} is inside the {warmup}-bar warm-up; "
                         f"pick a later bar or supply more history")

    signal = engine.evaluate(fs, index)
    if args.brief:
        print(signal.brief())
        return 0

    print(f"{series.symbol}  bar {index}  closed {series.time_at(index):%Y-%m-%d %H:%M} UTC")
    print(f"gate stack: {' -> '.join(g.name for g in engine.stack)}\n")
    print(signal.report())

    if signal.tradable:
        from .risk import RiskManager
        risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
        atr_rank = fs.values["atr_rank"][index]
        decision = risk.assess(bar_index=index, ts=signal.ts,
                               p_model=signal.p_model, atr_rank=float(atr_rank))
        print()
        print(decision.report())
        print(f"\nANSWER: {'YES' if decision.approved else 'NO'}"
              + (f" -- bet {signal.side_name} with {decision.stake:,.2f}"
                 if decision.approved else " -- signal fired but risk refused it"))
    else:
        print(f"\nANSWER: NO -- {', '.join(signal.blocked_by)}")
    return 0


def cmd_gates(args) -> int:
    cfg = _build_config(args)
    series = _load_series(args)
    _warn_if_synthetic(args)
    minute = _load_minute(args)
    _note_minute(cfg, minute)
    stats = gate_edge(series, cfg, reference=_load_reference(args), minute=minute)
    print(render_table(stats, "What is each gate's directional vote worth?"))
    print("\nRead z before accuracy: it is how many standard errors the hit rate")
    print("sits above break-even. Under +2, the gate has shown you nothing yet.")
    print("Gates can be worth less alone than in combination -- a filter that is")
    print("neutral on its own can still sharpen a stack. Check with `compare`.")
    return 0


def _number_list(text: str, cast, flag: str) -> list:
    try:
        values = [cast(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise SystemExit(f"{flag} expects comma-separated numbers, got {text!r}")
    if not values:
        raise SystemExit(f"{flag} is empty")
    return values


def cmd_flow(args) -> int:
    """Does who was aggressing just before the window opens predict it?"""
    cfg = _build_config(args)
    series = _load_series(args)
    _warn_if_synthetic(args)
    minute = _load_minute(args)
    windows = _number_list(args.windows, int, "--windows")
    thresholds = _number_list(args.thresholds, float, "--thresholds")
    floors = _number_list(args.volume_floors, float, "--volume-floors")
    if any(w < 0 for w in windows):
        raise SystemExit("--windows takes minutes >= 1, or 0 for the bar's own share")
    if any(x < 0 for x in floors):
        raise SystemExit("--volume-floors takes multiples of median volume >= 0")
    if series.taker_buy is None and 0 in windows:
        raise SystemExit("--data has no taker_buy column, so the full-bar share "
                         "cannot be read; fetch the series from binance")
    if minute is None:
        dropped = [w for w in windows if w > 0]
        windows = [w for w in windows if w == 0]
        if dropped:
            print(f"note: no --minute CSV, so the minute windows {dropped} are "
                  f"skipped; fetch one with `fetch --interval 1m`.",
                  file=sys.stderr)
        if not windows:
            raise SystemExit("nothing to measure: give --minute, or --windows 0")

    corrs = flow_correlation(series, cfg, minute, windows)
    cells = flow_edge(series, cfg, minute, windows, thresholds, floors)
    print(render_flow(corrs, cells,
                      "Does the taker share just before the open predict the window?"))
    print("\nRead the first table's sign before anything else: negative means the")
    print("flow reverted over the next window (bet against it: fade), positive")
    print("means it carried (follow). Inside the noise band it said nothing.")
    print("The grid then shows what a |z| floor buys: fewer signals, and whether")
    print("the ones that remain beat break-even. follow and fade are exact")
    print("complements on the same signals, so only the better side has a z.")
    print("This is a development-year measurement. Pick values from it, write")
    print("them into a config, and test that config once on unseen history.")
    return 0


def cmd_compare(args) -> int:
    series = _load_series(args)
    _warn_if_synthetic(args)
    base = to_dict(_build_config(args))
    base.pop("gate_stack", None)
    stacks = ({name: PRESETS[name] for name in args.presets} if args.presets
              else dict(PRESETS))
    stats = compare_stacks(series, stacks, base=base,
                           reference=_load_reference(args))
    print(render_table(stats, "Which gate stack should I run?"))
    print("\nMore gates is not better: every gate added cuts signal frequency,")
    print("and a stack that fires twice a month cannot prove anything. Look for")
    print("the best z at a frequency you can actually trade.")
    return 0


def cmd_overlap(args) -> int:
    cfg = _build_config(args)
    series = _load_series(args)
    _warn_if_synthetic(args)
    print(full_report(series, cfg, reference=_load_reference(args)))
    print()
    print("A stack is only as selective as the number of independent questions")
    print("it asks. Two gates that fire together for the same reason halve the")
    print("signal count without adding evidence, and make the stack look more")
    print("confirmed than it is. Section 3 is the one that decides.")
    return 0


def cmd_quote(args) -> int:
    """Price one live contract market against the latest bar's signal."""

    from .features import build_features
    from .risk import RiskManager
    from .signal import SignalEngine

    cfg = _build_config(args)
    # The config declares its venue; --venue overrides it explicitly.
    rules = VENUES[args.venue or cfg.venue]
    series = _load_series(args)
    _warn_if_synthetic(args)
    fs = build_features(series, cfg, _load_reference(args))
    engine = SignalEngine(cfg)
    index = args.bar if args.bar is not None else len(series) - 1
    if index < 0:
        index += len(series)
    if not 0 <= index < len(series):
        raise SystemExit(f"--bar {args.bar} is outside 0..{len(series) - 1}")
    if index < engine.warmup_bars(fs):
        raise SystemExit("not enough history: that bar is inside the warm-up")

    # The window opens at the close of the bar we are deciding on.
    window_start = int(series.ts[index])
    observed = window_start + args.into_window
    signal = engine.evaluate(fs, index, now_ts=observed)

    quote = MarketQuote.from_percent(window_start, args.down, observed_ts=observed,
                                     rules=rules, overround=args.overround)
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    atr_rank = float(fs.values["atr_rank"][index])
    decision = evaluate_market(signal, quote, cfg, risk=risk, rules=rules,
                               atr_rank=atr_rank)
    if args.brief:
        print(decision.brief())
        return 0

    print(f"venue    {rules.name}  ({rules.chain or 'off-chain'})")
    print(f"settles  {rules.price_feed}")
    print()
    print(signal.report())
    print()
    print(decision.report())
    if decision.edge == decision.edge and decision.edge > 0:
        floor = minimum_viable_stake(decision.edge, decision.price, rules)
        print(f"\nminimum viable stake at this edge: {floor:,.2f} "
              f"(3x the {rules.gas_cost_quote:,.2f} gas cost)")
    return 0


def cmd_probe(args) -> int:
    """Print a venue's real payload shapes so the field guesses can be fixed.

    predict.fun needs a key on mainnet; Polymarket's read side needs nothing.
    Neither path signs anything.
    """
    if args.venue == "polymarket-btc-5m":
        print(probe_polymarket(PolymarketClient(), limit=args.limit))
        return 0
    client = PredictFunClient(api_key=args.api_key, testnet=args.testnet)
    print(probe_predictfun(client, limit=args.limit))
    return 0


def cmd_live(args) -> int:
    """Price the currently open predict.fun window against the latest bar."""
    import time

    from .features import build_features
    from .risk import RiskManager
    from .signal import SignalEngine

    cfg = _build_config(args)
    rules = VENUES[args.venue or cfg.venue]
    client = PredictFunClient(api_key=args.api_key, testnet=args.testnet)

    market = client.current_btc_window()
    if market is None:
        raise SystemExit(
            "no five-minute BTC window is open right now. Run `btc5m probe` to "
            "check what the API is returning and whether the field names match.")

    series = _load_series(args)
    _warn_if_synthetic(args)
    fs = build_features(series, cfg, _load_reference(args))
    engine = SignalEngine(cfg)
    index = len(series) - 1
    if index < engine.warmup_bars(fs):
        raise SystemExit("not enough history: the last bar is inside the warm-up")

    now = int(time.time())
    quote = market.to_quote(observed_ts=now, rules=rules)
    signal = engine.evaluate(fs, index, now_ts=now)
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    decision = evaluate_market(signal, quote, cfg, risk=risk, rules=rules,
                               atr_rank=float(fs.values["atr_rank"][index]))

    if args.brief:
        print(decision.brief())
        return 0
    print(f"market   {market.title}  ({market.market_id})")
    print(f"bars     {series.symbol} through "
          f"{series.time_at(index):%Y-%m-%d %H:%M} UTC")
    print()
    print(decision.report())
    return 0


def _bar_feed(args):
    """A callable returning the freshest bars, for the watcher to call per window.

    A CSV is read once and reused, which is right for a dry run and wrong for a
    live session, so it says so.  A live feed is refetched every window, and
    Binance's mirror stands in for the main host when that is geo-blocked --
    which it is from most cloud regions, and which would otherwise leave the
    flow gate with no taker volume at all.
    """
    if getattr(args, "data", None):
        series = load_csv(args.data)
        print(f"bars      {args.data}: {len(series):,} static bars, last "
              f"{series.time_at(len(series) - 1):%Y-%m-%d %H:%M} UTC")
        print("          (a file does not refresh; every window will read the "
              "same last bar. Use --exchange for a live session.)")
        return lambda: series

    wanted = getattr(args, "exchange", None) or "binance"
    chain = [wanted] + (["binance-vision"] if wanted == "binance" else [])
    state = {"host": None}

    def feed():
        errors = []
        for host in ([state["host"]] if state["host"] else chain):
            try:
                series = fetch_klines(host, args.symbol, args.limit)
            except (RuntimeError, ValueError) as exc:
                errors.append(f"{host}: {exc}")
                continue
            if state["host"] != host:
                print(f"bars      {host}: {len(series):,} bars, last "
                      f"{series.time_at(len(series) - 1):%Y-%m-%d %H:%M} UTC")
                state["host"] = host
            return series
        state["host"] = None
        raise RuntimeError("; ".join(errors))

    return feed


def cmd_watch(args) -> int:
    """Log what the book offered when the gates fired.  Reads only; no orders."""
    cfg = _build_config(args)
    venue = args.venue or cfg.venue
    rules = VENUES[venue]
    if rules is not POLYMARKET_BTC_5M:
        raise SystemExit(
            f"watch reads Polymarket's public API, and the venue here is {venue}. "
            "Pass --venue polymarket-btc-5m, or a config that names it.")
    offsets = tuple(int(x) for x in args.offsets.split(",") if x.strip())
    watcher = Watcher(PolymarketClient(), cfg, _bar_feed(args), out=args.out,
                      offsets=offsets, rules=rules, reference=_load_reference(args))
    print(f"venue     {rules.name}   offsets {', '.join(f'+{o}s' for o in offsets)}")
    # The engine's break-even comes from the config's fee basis and the price
    # paid comes from the venue's own fee, so a mismatch is worth naming: it
    # changes which bars count as "the gates fired".  The config is not
    # rewritten -- several of them are pre-registered and must not be.
    print(f"break-even {cfg.break_even_probability():.4f} from the config; "
          f"the book is charged {rules.name}'s own fee "
          f"({rules.fee_per_share(0.5):.4f} a share at 0.50)")
    if venue != cfg.venue:
        print(f"          note: the config names {cfg.venue}, and its break-even "
              f"is the one the gates are judged against here.")
    print(f"writing   {args.out}")
    print("windows   until stopped" if args.windows == 0 else
          f"windows   {args.windows}  (about {args.windows * 5} minutes)")
    print()
    rows = watcher.run(windows=args.windows)
    print()
    print(f"{rows} rows written to {args.out}. "
          f"`btc5m settle --quotes {args.out}` once the windows have ended.")
    return 0


def cmd_settle(args) -> int:
    """Fill in who won, for windows that have ended."""
    filled, left = settle_quotes(PolymarketClient(), args.quotes)
    print(f"settled {filled} window(s); {left} ended window(s) still unresolved "
          f"at the venue")
    return 0


def cmd_report(args) -> int:
    """What the quote was when the gates fired, and what it would have paid."""
    rows = read_rows(args.quotes)
    break_even, rules = None, None
    if getattr(args, "config", None) or getattr(args, "set", None):
        cfg = _build_config(args)
        break_even = cfg.break_even_probability()
        rules = VENUES[args.venue or cfg.venue]
    print(render_report(rows, break_even=break_even, rules=rules))
    return 0


def _bets_by_window(args, cfg) -> tuple[dict[int, int], int]:
    """Every window the config would have bet, mapped to the side it would take.

    A signal on the bar closing at ``t`` bets the window that opens at ``t``, so
    the bar's own timestamp is the window's start.
    """
    from .features import build_features
    from .signal import SignalEngine

    series = load_csv(args.data)
    fs = build_features(series, cfg, _load_reference(args), _load_minute(args))
    engine = SignalEngine(cfg)
    bets = {}
    for i in range(engine.warmup_bars(fs), len(series) - 1):
        signal = engine.evaluate(fs, i)
        if signal.tradable:
            bets[int(series.ts[i])] = signal.side
    return bets, len(series)


def cmd_settlement(args) -> int:
    """How much the venue's settlement rule differs from the one every backtest used."""
    if not args.minute:
        raise SystemExit("settlement needs minute bars: --minute FILE.csv "
                         "(fetch them with `fetch --interval 1m --year`)")
    minute = load_csv(args.minute)
    prices = window_prices(minute)
    print(f"minute bars  {args.minute}: {len(minute):,} rows, "
          f"{len(prices):,} complete five-minute windows")
    if not len(prices):
        raise SystemExit("no complete window in those minute bars; "
                         "fetch them with `fetch --interval 1m`")
    print()

    bets, cfg = None, None
    if args.data:
        cfg = _build_config(args)
        bets, bars = _bets_by_window(args, cfg)
        print(f"config       {len(bets):,} of {bars:,} bars would have been bet "
              f"({100.0 * len(bets) / max(bars, 1):.2f}%)")
        print()
    print(render_comparison(compare_settlement(
        prices, bet_starts=None if bets is None else bets.keys())))

    # The number that decides it: not how often the rules differ, but whether the
    # side the strategy took is the one each rule pays.
    if bets:
        print()
        print(render_hit_rates(hit_rates(prices, bets),
                               break_even=cfg.break_even_probability()))

    if args.quotes:
        settled = {row["window_start"]: row["settled"] for row in read_rows(args.quotes)
                   if row.get("settled") and row.get("window_start")}
        print()
        print(render_venue_scores(*score_against_venue(prices, settled)))
    return 0


def _parse_end(text: str) -> datetime:
    """A --end date, read as midnight UTC on that day.

    The series then ends on the last bar closing at or before that instant, so
    two fetches with --end one year apart are contiguous and never overlap.
    """
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"error: --end must be YYYY-MM-DD, got {text!r}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def cmd_fetch(args) -> int:
    # Three paths, and the default picks between them because the right one is
    # not obvious: Binance's public archives are unmetered, 12 files for a year
    # instead of 106 requests, and -- unlike api.binance.com, which answers a US
    # IP with 451 -- not geo-restricted.  So a long Binance request goes there,
    # and the REST API is only for the recent tail or another exchange.
    bar_seconds = INTERVAL_SECONDS[args.interval]
    if args.year:
        args.limit = 365 * 86_400 // bar_seconds
    source = args.source
    if source == "auto":
        source = "dump" if (args.exchange == "binance"
                            and args.limit > 1000) else "api"

    # --end exists so a development set and a holdout can be cut from history
    # that nobody has looked at yet.  A backtest on the one year already
    # examined cannot validate anything derived from examining it.
    end = _parse_end(args.end) if args.end else None
    if end is not None and source == "api" and args.limit <= 1000:
        print("error: --end needs a paged fetch; the single-request path "
              "always returns the most recent bars. Raise --limit above 1000.",
              file=sys.stderr)
        return 2

    if source == "dump":
        if args.exchange != "binance":
            print(f"error: only binance publishes archives; "
                  f"--exchange {args.exchange} needs --source api",
                  file=sys.stderr)
            return 2

        def show_dump(done: int, total: int, held: int) -> None:
            print(f"\r  archive {done}/{total}, {held:,} bars",
                  end="", flush=True)

        series = fetch_binance_dump(args.symbol or "BTCUSDT", args.limit,
                                    pause_seconds=args.pause, now=end,
                                    progress=show_dump, interval=args.interval)
        print()
    elif args.limit > 1000:
        def show(held: int, wanted: int) -> None:
            print(f"\r  {held:,} / {wanted:,} bars", end="", flush=True)

        series = fetch_history(args.exchange, args.symbol, args.limit,
                               pause_seconds=args.pause, progress=show,
                               end_ts=int(end.timestamp()) if end else None,
                               interval=args.interval)
        print()
    else:
        series = fetch_klines(args.exchange, args.symbol, args.limit,
                              interval=args.interval)

    if len(series) < args.limit:
        where = "binance's archives" if source == "dump" else args.exchange
        print(f"note: {where} yielded {len(series):,} bars, not the "
              f"{args.limit:,} asked for -- either that is the whole history "
              f"for {series.symbol}, or a period is missing.")

    path = series.write_csv(args.out)
    days = len(series) * bar_seconds / 86_400
    print(f"wrote {len(series):,} closed {args.interval} bars ({days:.1f} days) "
          f"to {path}")
    print(f"  {series.time_at(0):%Y-%m-%d %H:%M} .. "
          f"{series.time_at(-1):%Y-%m-%d %H:%M} UTC")
    gaps = series.gaps()
    if gaps:
        missing = sum(n for _, n in gaps)
        print(f"  {len(gaps)} gap(s), {missing:,} bars missing; data_integrity "
              f"vetoes the bar after each one")
    return 0


def cmd_sample(args) -> int:
    series = synthetic(args.bars, seed=args.seed)
    path = series.write_csv(args.out)
    print(f"wrote {len(series)} synthetic 5m bars to {path}")
    print("These validate the machinery, not the edge.")
    return 0


def cmd_menu(args) -> int:
    print("GATE MENU -- factors you can require before betting\n")
    kinds = {"veto": "veto (no direction; refuses the bar outright)",
             "directional": "directional (votes UP or DOWN, carries a score)",
             "confirm": "confirmation (told the side, may only refuse it)"}
    for kind, label in kinds.items():
        print(f"{label}")
        for name, gate in sorted(GATE_REGISTRY.items()):
            if gate.kind != kind:
                continue
            doc = (gate.__doc__ or "").strip().splitlines()
            headline = doc[0] if doc else ""
            print(f"  {name:18s} {headline}")
            body = [ln.strip() for ln in doc[1:] if ln.strip()]
            for line in body:
                print(f"  {'':18s}   {line}")
        print()
    print("PRESET STACKS")
    for name, names in PRESETS.items():
        print(f"  {name:16s} {', '.join(names)}")
    print("\nMeasure before you commit: `btc5m gates` scores each gate on your own")
    print("data, `btc5m compare` scores whole stacks, and `btc5m overlap` checks")
    print("whether the gates you picked are actually asking different questions.")
    return 0


def cmd_config(args) -> int:
    print(json.dumps(to_dict(_build_config(args)), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc5m",
        description="Gated 5-minute binary up/down strategy for BTC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest", help="walk the series and report performance")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--json", metavar="FILE", help="also write results as JSON")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("signal", help="full gate trace for one bar")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--bar", type=int, help="bar index (default: the last one)")
    p.add_argument("--brief", action="store_true",
                   help="one line: the answer and, if no, the reason")
    p.set_defaults(func=cmd_signal)

    p = sub.add_parser("gates", help="score every gate's vote on your data")
    _add_data_args(p)
    _add_config_args(p)
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("flow", help="does the taker share just before the open "
                                    "predict the window?")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--windows", default="1,2,3,5,0", metavar="MIN,...",
                   help="minutes before the open to pool from --minute; 0 is "
                        "the 5m bar's own share (default: 1,2,3,5,0)")
    p.add_argument("--thresholds", default="1,1.5,2,2.5", metavar="Z,...",
                   help="|z| floors to score (default: 1,1.5,2,2.5)")
    p.add_argument("--volume-floors", default="0", metavar="X,...",
                   help="volume floors to score, as multiples of the bar's "
                        "rolling median volume; 0 = none (default: 0)")
    p.set_defaults(func=cmd_flow)

    p = sub.add_parser("compare", help="score candidate gate stacks")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--presets", nargs="+", choices=sorted(PRESETS),
                   help="limit the comparison to these presets")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("overlap", help="check whether gates and conditions are independent")
    _add_data_args(p)
    _add_config_args(p)
    p.set_defaults(func=cmd_overlap)

    p = sub.add_parser("quote", help="price a live contract market against the signal")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--down", type=float, required=True,
                   help="the DOWN price the venue is showing, as a percent (84) "
                        "or a probability (0.84)")
    p.add_argument("--into-window", type=int, default=5, metavar="SECONDS",
                   help="seconds elapsed since the window opened (default: 5)")
    p.add_argument("--overround", type=float, default=0.0,
                   help="how much the two sides cost above 1.00 together")
    p.add_argument("--venue", choices=sorted(VENUES), default=None,
                   help="override the venue declared by the config")
    p.add_argument("--bar", type=int, help="bar index (default: the last one)")
    p.add_argument("--brief", action="store_true",
                   help="one line: the answer and, if no, the reason")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("probe", help="show a venue's real API payload shapes")
    p.add_argument("--venue", choices=sorted(VENUES), default="predict-fun-btc-5m",
                   help="which venue's public API to read (default: predict.fun)")
    p.add_argument("--testnet", action="store_true",
                   help="predict.fun only: use api-testnet.predict.fun, which needs no API key")
    p.add_argument("--api-key", help="predict.fun mainnet key; also read from PREDICT_FUN_API_KEY")
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("watch", help="log the live quote when the gates fire "
                                     "(reads only, places nothing)")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--venue", choices=sorted(VENUES), default=None,
                   help="override the venue the config names (must be Polymarket)")
    p.add_argument("--windows", type=int, default=12,
                   help="how many five-minute windows to watch "
                        "(default: 12, an hour; 0 watches until stopped)")
    p.add_argument("--offsets", default=",".join(str(o) for o in DEFAULT_OFFSETS),
                   metavar="S,S,S",
                   help="seconds into each window to read the book "
                        f"(default: {','.join(str(o) for o in DEFAULT_OFFSETS)})")
    p.add_argument("--out", default="quotes.csv", metavar="CSV",
                   help="append observations here (default: quotes.csv)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("settle", help="fill in who won, for windows that ended")
    p.add_argument("--quotes", required=True, metavar="CSV",
                   help="a CSV written by `btc5m watch`")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("report", help="was the market near even when the gates fired?")
    p.add_argument("--quotes", required=True, metavar="CSV",
                   help="a CSV written by `btc5m watch`")
    p.add_argument("--venue", choices=sorted(VENUES), default=None,
                   help="price the rows against this venue's rules "
                        "(default: the one the config names)")
    _add_config_args(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("settlement",
                       help="does the venue's settlement rule pay the same side "
                            "as the close-to-close the backtests assume?")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--quotes", metavar="CSV",
                   help="a settled CSV from `btc5m watch`, to score the rules "
                        "against what the venue actually paid")
    p.set_defaults(func=cmd_settlement)

    p = sub.add_parser("live", help="price the open predict.fun window")
    _add_data_args(p)
    _add_config_args(p)
    p.add_argument("--testnet", action="store_true",
                   help="use api-testnet.predict.fun, which needs no API key")
    p.add_argument("--api-key", help="mainnet key; also read from PREDICT_FUN_API_KEY")
    p.add_argument("--venue", choices=sorted(VENUES), default=None)
    p.add_argument("--brief", action="store_true",
                   help="one line: the answer and, if no, the reason")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("fetch", help="download closed candles to CSV")
    p.add_argument("--exchange", default="binance",
                   choices=("binance", "coinbase", "kraken"))
    p.add_argument("--symbol")
    p.add_argument("--interval", default="5m", choices=sorted(INTERVAL_SECONDS),
                   help="candle length (default 5m). 1m is binance only and "
                        "is what --minute reads")
    p.add_argument("--limit", type=int, default=1000,
                   help="bars to fetch; above 1000 pages backwards "
                        "(a year is 105120 at 5m). kraken cannot page")
    p.add_argument("--year", action="store_true",
                   help="one year of bars: 105120 at 5m, 525600 at 1m")
    p.add_argument("--pause", type=float, default=0.25,
                   help="seconds between requests (default 0.25)")
    p.add_argument("--source", default="auto", choices=("auto", "dump", "api"),
                   help="dump = data.binance.vision archives (unmetered, not "
                        "geo-blocked); api = the REST endpoint. auto picks dump "
                        "for binance above 1000 bars")
    p.add_argument("--end", metavar="YYYY-MM-DD",
                   help="end the series at midnight UTC on this date instead "
                        "of now, to cut a development set and a holdout from "
                        "history nobody has examined yet")
    p.add_argument("-o", "--out", default="btc_5m.csv")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("sample", help="write synthetic bars to CSV")
    p.add_argument("--bars", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("-o", "--out", default="btc_5m_synthetic.csv")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("menu", help="list the gate menu and presets")
    p.set_defaults(func=cmd_menu)

    p = sub.add_parser("config", help="print the effective config as JSON")
    _add_config_args(p)
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, RuntimeError, FileNotFoundError,
            PredictFunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
