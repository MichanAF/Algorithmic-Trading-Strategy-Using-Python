"""Watch live windows and record what the book offered when the gates fired.

The whole strategy rests on one unmeasured assumption: that when the stack says
UP or DOWN, the market is still priced near even.  A backtest cannot test it,
because it prices every bet at a quote the backtest itself invented.  This
module measures it, and nothing else:

    watch     every window, at a few seconds into it, read both sides' books
              and the engine's verdict on the bar that just closed, and append
              one row per reading to a CSV
    settle    once a window has ended, read who won from the venue and fill
              that column in
    report    what the quote looked like when the gates fired, versus when they
              did not, and what the edge would have been at the real price

It places no orders and holds no key.  ``watch`` only reads; ``settle`` only
reads; the CSV is the deliverable.  What it answers, in order of how likely it
is to end the project:

1. Is the quote near 50/50 in the first seconds of a window?  If the market has
   already moved to 0.93 by the time the bar closes, there is no bet to make.
2. Does it move against the signal's side specifically?  A market that is even
   on average but always dear on the side the stack wants is worse than a market
   that is always dear.
3. Is the bar the engine reads actually the bar that just closed?  A watcher
   whose data feed lags by a bar is measuring the wrong window, and the CSV
   records the lag rather than hiding it.
"""

from __future__ import annotations

import csv
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Sequence

from .config import StrategyConfig
from .data import BarSeries
from .features import build_features
from .polymarket import WINDOW_SECONDS, Book, PolyMarket, PolymarketClient, PolymarketError, window_start
from .risk import RiskManager
from .signal import SignalEngine
from .venue import POLYMARKET_BTC_5M, DOWN, FLAT, UP, MarketQuote, VenueRules, evaluate_market

DEFAULT_OFFSETS = (5, 15, 30)


@dataclass
class Observation:
    """One reading of one window: what the book said, what the engine said."""

    window_start: int
    window_time: str
    offset: int
    observed_ts: int
    slug: str = ""
    up_bid: float | None = None
    up_ask: float | None = None
    down_bid: float | None = None
    down_ask: float | None = None
    up_ask_depth: float | None = None
    down_ask_depth: float | None = None
    overround: float | None = None
    skew: float | None = None
    bar_ts: int | None = None
    bar_lag: int | None = None          # window_start - bar_ts; 0 is correct
    bar_return: float | None = None
    side: str = ""
    conviction: float | None = None
    p_model: float | None = None
    tradable: str = ""                  # "yes"/"no": did the stack propose a bet
    price: float | None = None          # what that side costs, fee included
    edge: float | None = None           # p_model - price
    approved: str = ""                  # did every betting and risk condition pass
    stake: float | None = None
    reason: str = ""
    settled: str = ""                   # "UP"/"DOWN", filled by settle()
    note: str = ""                      # why a row is thin, if it is

    @classmethod
    def csv_fields(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _note(obs: "Observation", text: str, limit: int = 300) -> None:
    """Add a reason to a row without losing the one already there.

    A window can fail in more than one way at once -- no market *and* no bars --
    and the first draft overwrote the earlier reason with the later one, which
    made a two-fault window look like a one-fault window.
    """
    text = text.strip()
    if not text or text in obs.note:
        return
    obs.note = f"{obs.note}; {text}"[:limit] if obs.note else text[:limit]


def append_row(path: str | Path, obs: Observation) -> Path:
    """Append one observation, writing the header if the file is new.

    Append rather than rewrite: a watcher that is killed mid-session keeps
    everything it measured, and a second session continues the same file.
    """
    path = Path(path)
    new = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=Observation.csv_fields())
        if new:
            writer.writeheader()
        writer.writerow(asdict(obs))
    return path


def read_rows(path: str | Path) -> list[dict[str, Any]]:
    """Read a watch CSV back, numbers as numbers and blanks as None."""
    ints = {"window_start", "offset", "observed_ts", "bar_ts", "bar_lag"}
    floats = {"up_bid", "up_ask", "down_bid", "down_ask", "up_ask_depth",
              "down_ask_depth", "overround", "skew", "bar_return", "conviction",
              "p_model", "price", "edge", "stake"}
    out = []
    with Path(path).open(newline="") as fh:
        for raw in csv.DictReader(fh):
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if key is None:
                    continue
                text = (value or "").strip()
                if key in ints or key in floats:
                    if text in ("", "None"):
                        row[key] = None
                    else:
                        try:
                            row[key] = int(text) if key in ints else float(text)
                        except ValueError:
                            row[key] = None
                else:
                    row[key] = text
            out.append(row)
    return out


# --------------------------------------------------------------------------- #
# the watcher
# --------------------------------------------------------------------------- #


class Watcher:
    """Reads one window at a time and appends what it saw.

    Everything that moves is injected, so a test drives a whole session with no
    network and no waiting: ``now`` and ``sleep`` are the clock, ``fetch_series``
    is the bar feed, ``client`` is the venue.
    """

    def __init__(self, client: PolymarketClient, cfg: StrategyConfig,
                 fetch_series: Callable[[], BarSeries], *,
                 out: str | Path = "quotes.csv",
                 offsets: Sequence[int] = DEFAULT_OFFSETS,
                 rules: VenueRules = POLYMARKET_BTC_5M,
                 reference: BarSeries | None = None,
                 now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print,
                 alert: bool = False):
        if not offsets:
            raise ValueError("at least one offset is needed")
        if any(o < 0 or o >= WINDOW_SECONDS for o in offsets):
            raise ValueError(f"offsets must be inside the window (0-{WINDOW_SECONDS - 1}s)")
        self.client = client
        self.cfg = cfg
        self.fetch_series = fetch_series
        self.out = Path(out)
        self.offsets = tuple(sorted(offsets))
        self.rules = rules
        self.reference = reference
        self.now = now
        self.sleep = sleep
        self.log = log
        self.engine = SignalEngine(cfg)
        self.risk = RiskManager(cfg.risk, self.engine.break_even, self.engine.odds)
        self.alert = alert
        self.rows = 0
        self.alerts = 0
        self._series: BarSeries | None = None
        self._series_for: int | None = None   # the window the bars were fetched for
        self._market: PolyMarket | None = None
        self._market_for: int | None = None

    # -- the session ----------------------------------------------------- #

    def run(self, windows: int = 1) -> int:
        """Watch ``windows`` windows and return how many rows were written.

        ``windows=0`` never returns: that is the always-on case, where the
        process is stopped by whatever supervises it rather than by a count.
        """
        forever = windows == 0
        seen = 0
        done: int | None = None          # the window whose readings are finished
        while forever or seen < windows:
            start = window_start(int(self.now()))
            # The last reading of a window lands on its own boundary second, so
            # without this the loop would read that offset again and again.
            pending = ([] if start == done
                       else [o for o in self.offsets if start + o >= int(self.now())])
            if not pending:
                self._sleep_until(start + WINDOW_SECONDS + self.offsets[0])
                continue
            for offset in pending:
                self._sleep_until(start + offset)
                self.observe(start, offset)
            done = start
            seen += 1
        return self.rows

    def _sleep_until(self, target: int) -> None:
        delay = target - self.now()
        if delay > 0:
            self.sleep(delay)

    # -- one reading ----------------------------------------------------- #

    def observe(self, start: int, offset: int) -> Observation:
        obs = Observation(window_start=start, window_time=_iso(start), offset=offset,
                          observed_ts=int(self.now()))
        market = self._market_at(start, obs)
        books = self._books(market, obs) if market is not None else None
        self._signal_into(obs, start, market, books)
        append_row(self.out, obs)
        self.rows += 1
        self.log(self._line(obs))
        if self.alert and obs.approved == "yes":
            self.alerts += 1
            self.log(self._alert(obs, market))
        return obs

    def _alert(self, obs: Observation, market: PolyMarket | None) -> str:
        """The instruction, for a person placing the bet by hand.

        Everything needed to act is on the screen, because the entry budget is
        thirty seconds and nobody should be doing arithmetic inside it.  The
        price shown is the ask including the venue's fee; the limit is the raw
        ask, which is what the venue's own ticket asks for.
        """
        raw = obs.up_ask if obs.side == "UP" else obs.down_ask
        shares = None if not raw or not obs.stake else obs.stake / raw
        left = obs.window_start + self.rules.max_entry_seconds - obs.observed_ts
        depth = obs.up_ask_depth if obs.side == "UP" else obs.down_ask_depth
        lines = [
            "",
            "  " + "=" * 68,
            f"  BET NOW: {obs.side}   window {obs.window_time} UTC",
            "  " + "-" * 68,
            f"    buy          {obs.side}  at  {raw:.2f}   (limit price)"
            if raw else f"    buy          {obs.side}",
            f"    stake        ${obs.stake:,.2f}"
            + (f"  ->  {shares:.0f} shares" if shares else ""),
            f"    all-in cost  {obs.price:.4f} a share, fee included"
            if obs.price is not None else "",
            f"    model says   {obs.p_model:.1%}   edge {obs.edge:+.1%}"
            if obs.p_model is not None and obs.edge is not None else "",
            f"    depth        {depth:.0f} shares at that ask" if depth else "",
            # Always positive: the timing condition refuses the bet outside the
            # entry budget, so an alert can only fire inside it.
            f"    entry closes in {left}s",
        ]
        if market is not None and market.slug:
            lines.append(f"    market       https://polymarket.com/event/{market.slug}")
        lines += ["  " + "=" * 68, ""]
        return "\a" + "\n".join(line for line in lines if line != "")

    def _market_at(self, start: int, obs: Observation) -> PolyMarket | None:
        """The window's market, fetched once per window and reused per offset."""
        if self._market_for != start:
            try:
                self._market = self.client.market_for_window(start)
            except PolymarketError as exc:
                self._market = None
                _note(obs, f"market: {exc}")
            self._market_for = start
        if self._market is None:
            _note(obs, "no market at that slug")
        if self._market is not None:
            obs.slug = self._market.slug
        return self._market

    def _books(self, market: PolyMarket, obs: Observation) -> tuple[Book, Book] | None:
        up_token, down_token = market.token_for("Up"), market.token_for("Down")
        if up_token is None or down_token is None:
            _note(obs, f"outcomes are {market.outcomes}, not Up/Down")
            return None
        try:
            up, down = self.client.book(up_token), self.client.book(down_token)
        except PolymarketError as exc:
            _note(obs, f"book: {exc}")
            return None
        obs.up_bid, obs.up_ask = up.best_bid, up.best_ask
        obs.down_bid, obs.down_ask = down.best_bid, down.best_ask
        obs.up_ask_depth, obs.down_ask_depth = up.ask_depth(), down.ask_depth()
        if up.best_ask is not None and down.best_ask is not None:
            obs.overround = round(up.best_ask + down.best_ask - 1.0, 6)
            obs.skew = round(abs(up.best_ask - down.best_ask) / 2.0, 6)
        else:
            _note(obs, "a side has no asks")
        return up, down

    def _signal_into(self, obs: Observation, start: int, market: PolyMarket | None,
                     books: tuple[Book, Book] | None) -> None:
        series = self._bars(start, obs)
        if series is None:
            return
        index = len(series) - 1
        obs.bar_ts = int(series.ts[index])
        obs.bar_lag = start - obs.bar_ts
        if index >= 1:
            obs.bar_return = round(float(series.close[index] / series.close[index - 1] - 1.0), 8)

        fs = build_features(series, self.cfg, self.reference)
        warmup = self.engine.warmup_bars(fs)
        if index < warmup:
            _note(obs, f"warm-up: {index} bars, {warmup} needed")
            return
        signal = self.engine.evaluate(fs, index, now_ts=obs.observed_ts)
        obs.side = signal.side_name
        obs.conviction = round(signal.conviction, 4)
        obs.p_model = round(signal.p_model, 6)
        obs.tradable = "yes" if signal.tradable else "no"
        obs.reason = signal.brief()

        # The priced decision needs both sides' asks; without them the row still
        # carries the engine's verdict, which is the half that is never missing.
        if books is None or obs.up_ask is None or obs.down_ask is None:
            return
        quote = MarketQuote(window_start_ts=start, up_price=obs.up_ask,
                            down_price=obs.down_ask, observed_ts=obs.observed_ts,
                            rules=self.rules)
        decision = evaluate_market(signal, quote, self.cfg, risk=self.risk,
                                   rules=self.rules,
                                   atr_rank=float(fs.values["atr_rank"][index]))
        if signal.side != FLAT:
            obs.price = round(decision.price, 6)
            obs.edge = round(decision.edge, 6)
        obs.approved = "yes" if decision.approved else "no"
        obs.stake = round(decision.stake, 4)
        if not decision.approved and decision.blocked_by:
            obs.reason = f"{obs.reason} | blocked: {', '.join(decision.blocked_by)}"

    def _bars(self, start: int, obs: Observation) -> BarSeries | None:
        """The bar feed, refreshed once per window."""
        if self._series_for != start:
            try:
                self._series = self.fetch_series()
            except Exception as exc:                 # a feed can fail any way
                self._series = None
                _note(obs, f"bars: {exc}")
            self._series_for = start
        if self._series is None:
            _note(obs, "no bars")
            return None
        return self._series

    @staticmethod
    def _line(obs: Observation) -> str:
        def price(value):
            return "  --" if value is None else f"{value:.2f}"
        head = (f"{obs.window_time} +{obs.offset:>2}s  "
                f"UP {price(obs.up_bid)}/{price(obs.up_ask)}  "
                f"DOWN {price(obs.down_bid)}/{price(obs.down_ask)}")
        # The bar being faded, because the whole question is whether the market
        # has priced that move already and which side is therefore dear.
        bar = ("" if obs.bar_return is None
               else f"  bar {obs.bar_return * 100:+.2f}%")
        if obs.side in ("UP", "DOWN"):
            body = (f"  {obs.side} p {obs.p_model:.3f}"
                    f" price {price(obs.price)} edge "
                    + ("  --" if obs.edge is None else f"{obs.edge:+.3f}")
                    + f"  bet {obs.approved}")
        else:
            body = f"  {obs.reason or obs.note or 'no signal'}"
        lag = "" if obs.bar_lag in (None, 0) else f"  [bar lag {obs.bar_lag}s]"
        return head + bar + body + lag


# --------------------------------------------------------------------------- #
# settlement
# --------------------------------------------------------------------------- #


def settle(client: PolymarketClient, path: str | Path,
           now: int | None = None) -> tuple[int, int]:
    """Fill ``settled`` for every window that has ended.  Returns (filled, left).

    One venue call per unsettled window, not per row, and rows already settled
    are never re-read, so running it repeatedly is cheap and idempotent.
    """
    path = Path(path)
    rows = read_rows(path)
    if not rows:
        return 0, 0
    now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
    wanted = sorted({r["window_start"] for r in rows
                     if not r.get("settled") and r.get("window_start")
                     and r["window_start"] + WINDOW_SECONDS <= now})
    winners: dict[int, str] = {}
    for start in wanted:
        try:
            market = client.market_for_window(start)
        except PolymarketError:
            continue
        side = market.settled_side() if market is not None else None
        if side:
            winners[start] = side
    if winners:
        for row in rows:
            if not row.get("settled") and row.get("window_start") in winners:
                row["settled"] = winners[row["window_start"]]
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=Observation.csv_fields())
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if row.get(k) is None else row.get(k))
                                 for k in Observation.csv_fields()})
    left = len({r["window_start"] for r in rows
                if not r.get("settled") and r.get("window_start")}) - len(winners)
    return len(winners), max(left, 0)


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def _med(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return median(kept) if kept else None


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "--" if value is None else format(value, spec)


def _cheap_dear(row: dict[str, Any]) -> tuple[float, float] | None:
    """(cheaper ask, dearer ask), or None if the row is not priced on both sides.

    Which *named* side is dear varies window to window, so a median of the UP
    column and a median of the DOWN column describe nothing: they average a
    dear side with a cheap one. Cheap and dear are the direction-free pair, and
    they are what a buyer actually faces.
    """
    up, down = row.get("up_ask"), row.get("down_ask")
    if up is None or down is None:
        return None
    return (up, down) if up <= down else (down, up)


def render_report(rows: Sequence[dict[str, Any]], break_even: float | None = None,
                  rules: VenueRules | None = None) -> str:
    """What the quote was, and whether the signal's side was the dear one."""
    if not rows:
        return "no rows"
    lines: list[str] = []
    windows = {r["window_start"] for r in rows if r.get("window_start")}
    priced = [r for r in rows if r.get("up_ask") is not None and r.get("down_ask") is not None]
    lagged = [r for r in rows if r.get("bar_lag") not in (None, 0)]
    thin = [r for r in rows if r.get("note")]
    lines.append(f"rows {len(rows)}   windows {len(windows)}   "
                 f"both sides priced {len(priced)}   "
                 f"rows with a note {len(thin)}   bar lag on {len(lagged)}")
    if windows:
        lines.append(f"span {_iso(min(windows))} -> {_iso(max(windows) + WINDOW_SECONDS)} UTC")
    # A market further from even than the venue's entry limit cannot be bet at
    # all, whatever the signal says, so it is a cost of the venue and not of
    # the strategy.
    if rules is not None and priced:
        limit = rules.max_entry_skew
        decided = [r for r in priced if (r.get("skew") or 0.0) > limit]
        lines.append(f"already too decided to enter (skew above the venue's "
                     f"{limit:.2f}): {len(decided)}/{len(priced)}"
                     + (f" ({100.0 * len(decided) / len(priced):.0f}%)" if priced else ""))
    lines.append("")

    # 1. The quote itself, per offset.  The question is whether a window opens
    #    near even and how fast it stops being even.
    lines.append("the book, by seconds into the window")
    lines.append("  offset  n   cheap ask   dear ask   overround   skew   "
                 "|  ask depth, cheap/dear")
    for offset in sorted({r["offset"] for r in rows if r.get("offset") is not None}):
        at = [r for r in priced if r.get("offset") == offset]
        pairs = [pair for pair in (_cheap_dear(r) for r in at) if pair]
        if not at or not pairs:
            continue
        # Depth follows the price, not the name: the cheap side's depth is the
        # depth of whichever side is cheap in that reading.
        depths = []
        for r in at:
            pair = _cheap_dear(r)
            if pair is None:
                continue
            up_first = (r["up_ask"] <= r["down_ask"])
            depths.append((r["up_ask_depth"] if up_first else r["down_ask_depth"],
                           r["down_ask_depth"] if up_first else r["up_ask_depth"]))
        lines.append(f"  +{offset:>3}s  {len(at):<3} "
                     f"{_fmt(_med(c for c, _ in pairs), '.3f'):>10} "
                     f"{_fmt(_med(d for _, d in pairs), '.3f'):>10} "
                     f"{_fmt(_med(r['overround'] for r in at), '+.4f'):>11} "
                     f"{_fmt(_med(r['skew'] for r in at), '.3f'):>6}   |  "
                     f"{_fmt(_med(c for c, _ in depths), '.0f')}/"
                     f"{_fmt(_med(d for _, d in depths), '.0f')}")
    near = [r for r in priced if r.get("skew") is not None and r["skew"] <= 0.05]
    lines.append(f"  within 5 points of even: {len(near)}/{len(priced)}"
                 + (f" ({100.0 * len(near) / len(priced):.0f}%)" if priced else ""))
    all_pairs = [pair for pair in (_cheap_dear(r) for r in priced) if pair]
    if all_pairs and rules is not None:
        cheap, dear = _med(c for c, _ in all_pairs), _med(d for _, d in all_pairs)
        lines.append(f"  what each side needs to break even, all in: "
                     f"cheap {rules.effective_price(cheap):.3f}, "
                     f"dear {rules.effective_price(dear):.3f}")
    lines.append("")

    # The decisive question.  The signal fades the bar that just closed, so if
    # the market prices that move continuing, the side the stack wants is the
    # cheap one and an eight-point skew is a discount rather than a wall.
    lines.append("the dear side, against the bar just closed")
    paired = [r for r in priced
              if r.get("bar_return") not in (None, 0.0)
              and _cheap_dear(r) is not None
              and r["up_ask"] != r["down_ask"]]
    if not paired:
        lines.append("  no reading has both a move to price and a two-sided book")
    else:
        same = [r for r in paired
                if (r["up_ask"] > r["down_ask"]) == (r["bar_return"] > 0)]
        other = [r for r in paired if r not in same]
        share = 100.0 * len(same) / len(paired)
        lines.append(f"  readings with a move to price: {len(paired)}")
        lines.append(f"  the dear side is the way the bar moved: {len(same)}/{len(paired)} "
                     f"({share:.0f}%)   median skew {_fmt(_med(r['skew'] for r in same))}")
        lines.append(f"  the dear side is against it:            {len(other)}/{len(paired)} "
                     f"({100.0 - share:.0f}%)   median skew {_fmt(_med(r['skew'] for r in other))}")
        lines.append("  Above half means the market prices continuation, so the fade "
                     "side is the cheap one.")
        lines.append("  Below half means the fade is priced in and the edge is "
                     "being bought at the dear price.")
    lines.append("")

    # 2. The same, split by whether the stack proposed a bet.  This is the
    #    measurement the whole module exists for.
    fired = [r for r in priced if r.get("tradable") == "yes"]
    quiet = [r for r in priced if r.get("tradable") == "no"]
    lines.append("when the gates fired, versus when they did not")
    lines.append("  group          n    median skew   median price paid   median edge")
    for label, group in (("gates fired", fired), ("no signal", quiet)):
        if not group:
            lines.append(f"  {label:<13} {0:<4} --")
            continue
        lines.append(f"  {label:<13} {len(group):<4} "
                     f"{_fmt(_med(r['skew'] for r in group), '.3f'):>11} "
                     f"{_fmt(_med(r['price'] for r in group), '.3f'):>19} "
                     f"{_fmt(_med(r['edge'] for r in group), '+.3f'):>13}")
    if fired:
        positive = [r for r in fired if (r.get("edge") or 0.0) > 0]
        approved = [r for r in fired if r.get("approved") == "yes"]
        lines.append(f"  a positive edge at the real price: {len(positive)}/{len(fired)}"
                     f"   every condition passed: {len(approved)}/{len(fired)}")
        # The direct form of the question above, once there are signals to ask it of.
        wanted_cheap = []
        for r in fired:
            pair = _cheap_dear(r)
            if pair is None or r["up_ask"] == r["down_ask"] or r.get("side") not in ("UP", "DOWN"):
                continue
            cheap_side = "UP" if r["up_ask"] <= r["down_ask"] else "DOWN"
            wanted_cheap.append(r["side"] == cheap_side)
        if wanted_cheap:
            lines.append(f"  the side the stack wanted was the cheap one: "
                         f"{sum(wanted_cheap)}/{len(wanted_cheap)}")
        if break_even is not None:
            lines.append(f"  break-even at this venue: {break_even:.4f}")
    else:
        # 4.4% of bars is the pooled config's rate, so a short session firing
        # nothing is the expected outcome and not a finding.
        lines.append("  No signal fired. At the pooled config's rate of about one "
                     "bar in twenty, a session needs roughly 230 windows (nineteen "
                     "hours) to expect ten.")
    lines.append("")

    # 3. Settled windows: was the side right, and what would it have paid?
    settled = [r for r in fired if r.get("settled") and r.get("price") is not None]
    lines.append("settled windows where the gates fired")
    if not settled:
        lines.append("  none yet -- run `btc5m settle` once the windows have ended")
        return "\n".join(lines)
    wins = [r for r in settled if r["side"] == r["settled"]]
    pnl = [(1.0 - r["price"]) if r["side"] == r["settled"] else -r["price"]
           for r in settled]
    total = sum(pnl)
    lines.append(f"  bets {len(settled)}   right {len(wins)} "
                 f"({100.0 * len(wins) / len(settled):.1f}%)   "
                 f"P&L per contract {total / len(settled):+.4f}   "
                 f"total {total:+.3f} per contract staked")
    if break_even is not None:
        lines.append(f"  the hit rate needed at this price: "
                     f"{_med(r['price'] for r in settled):.3f} "
                     f"(the median price paid); at break-even {break_even:.4f}")
    lines.append("  A dozen windows is an anecdote, not a measurement: the hit "
                 "rate here has a standard error of about 14 points on 12 bets.")
    return "\n".join(lines)
