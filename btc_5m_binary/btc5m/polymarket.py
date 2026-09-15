"""Polymarket, read side only: the public Gamma and CLOB APIs.

Two hosts, no keys, no signing, no orders:

    gamma-api.polymarket.com   market and event metadata (what exists, when it
                               opens and closes, which two token ids trade)
    clob.polymarket.com        the order book for a token id (what a share
                               costs right now)

Both answer unauthenticated GETs.  Nothing in this module can move money: the
CLOB's order endpoints need a signed API key and a funded wallet, and neither
is anywhere in this repository.

Field names are best guesses until ``btc5m probe --venue polymarket`` has run
against the live API, exactly as predict.fun's were: the probe prints raw
payloads so the guesses can be corrected from evidence rather than memory.
Gamma is known to encode some list fields as JSON *strings* (``outcomes``,
``outcomePrices``, ``clobTokenIds``), so every list here is read through a
decoder that accepts either form.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .predictfun import _as_epoch, _as_price, _looks_like_btc
from .venue import POLYMARKET_BTC_5M, MarketQuote, VenueRules

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# The words a five-minute BTC up/down market's question or slug carries.  The
# probe reports what the live ones actually say; widen this from evidence.
_UP_DOWN_WORDS = ("up or down", "up-or-down", "updown", "up/down")

# Polymarket's series markets are addressed by slug, and the slug of a timed
# window appears to carry the window's start as a unix timestamp.  These are
# the candidate shapes for a five-minute BTC window starting at ``ts``; the
# probe tries each against the live API and reports which, if any, answers.
_SLUG_PATTERNS = ("btc-updown-5m-{ts}", "btc-up-or-down-5m-{ts}",
                  "bitcoin-up-or-down-5m-{ts}", "btc-usd-updown-5m-{ts}")
WINDOW_SECONDS = 300


def window_start(now: int, seconds: int = WINDOW_SECONDS) -> int:
    """The start of the window containing ``now``: five-minute boundaries."""
    return now - now % seconds


class PolymarketError(RuntimeError):
    pass


def _json_list(value: Any) -> list:
    """A list, whether Gamma sent a list or a JSON-encoded string of one."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                loaded = json.loads(text)
                return loaded if isinstance(loaded, list) else []
            except json.JSONDecodeError:
                return []
        return [text] if text else []
    return []


def _first(payload: dict, *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# --------------------------------------------------------------------------- #
# markets
# --------------------------------------------------------------------------- #


@dataclass
class PolyMarket:
    """One Polymarket binary market, as Gamma describes it."""

    market_id: str
    question: str
    slug: str
    condition_id: str
    outcomes: list[str]
    token_ids: list[str]
    outcome_prices: list[float | None]
    starts_at: int | None
    ends_at: int | None
    active: bool
    closed: bool
    accepting_orders: bool
    best_bid: float | None = None          # Gamma's own view of outcome 0's book
    best_ask: float | None = None
    tick: float | None = None
    min_order: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict) -> "PolyMarket":
        outcomes = [str(o) for o in _json_list(_first(payload, "outcomes"))]
        prices = [_as_price(p) for p in _json_list(_first(payload, "outcomePrices"))]
        tokens = [str(t) for t in _json_list(_first(payload, "clobTokenIds",
                                                     "clob_token_ids", "tokenIds"))]
        # A market nested in an event may carry its window on the event.
        events = _first(payload, "events") or []
        event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else {}
        starts = _first(payload, "startDate", "start_date", "startDateIso", "gameStartTime")
        ends = _first(payload, "endDate", "end_date", "endDateIso")
        if starts is None:
            starts = _first(event, "startDate", "start_date")
        if ends is None:
            ends = _first(event, "endDate", "end_date")
        return cls(
            market_id=str(_first(payload, "id", "marketId") or ""),
            question=str(_first(payload, "question", "title") or ""),
            slug=str(_first(payload, "slug", "marketSlug") or ""),
            condition_id=str(_first(payload, "conditionId", "condition_id") or ""),
            outcomes=outcomes, token_ids=tokens, outcome_prices=prices,
            starts_at=_as_epoch(starts), ends_at=_as_epoch(ends),
            active=_as_bool(_first(payload, "active")),
            closed=_as_bool(_first(payload, "closed")),
            accepting_orders=_as_bool(_first(payload, "acceptingOrders",
                                             "accepting_orders")),
            best_bid=_as_price(_first(payload, "bestBid", "best_bid")),
            best_ask=_as_price(_first(payload, "bestAsk", "best_ask")),
            tick=_as_price(_first(payload, "orderPriceMinTickSize")),
            min_order=_as_price(_first(payload, "orderMinSize")),
            raw=payload,
        )

    @property
    def window_seconds(self) -> int | None:
        if self.starts_at is None or self.ends_at is None:
            return None
        return self.ends_at - self.starts_at

    def is_five_minute(self) -> bool:
        return self.window_seconds == 300

    def is_up_down(self) -> bool:
        text = f"{self.question} {self.slug}".lower()
        return any(w in text for w in _UP_DOWN_WORDS)

    def is_btc(self) -> bool:
        return _looks_like_btc(self.question) or "btc" in self.slug.lower()

    def is_open(self, now: int | None = None) -> bool:
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        if self.starts_at is None or self.ends_at is None:
            return False
        return self.starts_at <= now < self.ends_at and not self.closed

    def token_for(self, side: str) -> str | None:
        """The token id that pays 1.00 if ``side`` ('Up' or 'Down') wins."""
        want = side.strip().lower()
        for name, token in zip(self.outcomes, self.token_ids):
            if name.strip().lower() == want:
                return token
        return None


# --------------------------------------------------------------------------- #
# order book
# --------------------------------------------------------------------------- #


@dataclass
class Book:
    """One token's order book, best levels first."""

    token_id: str
    bids: list[tuple[float, float]]        # (price, size), best (highest) first
    asks: list[tuple[float, float]]        # (price, size), best (lowest) first
    timestamp: int | None = None

    @classmethod
    def from_payload(cls, payload: dict, token_id: str = "") -> "Book":
        def levels(rows: Any) -> list[tuple[float, float]]:
            out = []
            for row in _json_list(rows):
                if not isinstance(row, dict):
                    continue
                price = _as_price(_first(row, "price"))
                size = _first(row, "size")
                try:
                    size = float(size) if size is not None else 0.0
                except (TypeError, ValueError):
                    size = 0.0
                if price is not None:
                    out.append((price, size))
            return out
        bids = sorted(levels(_first(payload, "bids")), key=lambda x: -x[0])
        asks = sorted(levels(_first(payload, "asks")), key=lambda x: x[0])
        ts = _first(payload, "timestamp")
        try:
            ts_int = int(float(ts)) if ts is not None else None
            if ts_int is not None and ts_int > 1e11:      # milliseconds
                ts_int //= 1000
        except (TypeError, ValueError):
            ts_int = None
        return cls(token_id=str(_first(payload, "asset_id", "token_id") or token_id),
                   bids=bids, asks=asks, timestamp=ts_int)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def ask_depth(self, up_to: float | None = None) -> float:
        """Shares offered at the best ask, or at or below ``up_to``."""
        if not self.asks:
            return 0.0
        limit = self.asks[0][0] if up_to is None else up_to
        return sum(size for price, size in self.asks if price <= limit + 1e-12)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


class PolymarketClient:
    """Minimal read-only client over the two public hosts."""

    def __init__(self, gamma_url: str = GAMMA, clob_url: str = CLOB,
                 timeout: int = 15):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.timeout = timeout

    def _get(self, base: str, path: str, **params) -> Any:
        query = {k: v for k, v in params.items() if v is not None}
        url = f"{base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, headers={"User-Agent": "btc5m/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise PolymarketError(f"{base} returned {exc.code} for {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PolymarketError(
                f"could not reach {base} ({exc}). Sandbox and corporate network "
                "policy often blocks it; run from a machine with direct access.") from exc

    @staticmethod
    def _items(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("data", "markets", "events", "results", "items"):
                inner = payload.get(key)
                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]
            return [payload] if payload else []
        return []

    # -- Gamma ----------------------------------------------------------- #

    def markets(self, limit: int = 50, **params) -> list[dict]:
        """Newest active markets first.  Falls back if Gamma rejects the ordering."""
        base = dict(active="true", closed="false", limit=limit)
        try:
            return self._items(self._get(self.gamma_url, "/markets",
                                         order="startDate", ascending="false",
                                         **base, **params))
        except PolymarketError:
            return self._items(self._get(self.gamma_url, "/markets", **base, **params))

    def events(self, limit: int = 50, **params) -> list[dict]:
        base = dict(active="true", closed="false", limit=limit)
        try:
            return self._items(self._get(self.gamma_url, "/events",
                                         order="startDate", ascending="false",
                                         **base, **params))
        except PolymarketError:
            return self._items(self._get(self.gamma_url, "/events", **base, **params))

    def market_by_slug(self, slug: str) -> PolyMarket | None:
        items = self._items(self._get(self.gamma_url, "/markets", slug=slug))
        return PolyMarket.from_payload(items[0]) if items else None

    def event_by_slug(self, slug: str) -> list[PolyMarket]:
        """The markets nested in the event at ``slug``, with the event's window."""
        found = []
        for event in self._items(self._get(self.gamma_url, "/events", slug=slug)):
            for raw in _json_list(event.get("markets")):
                if isinstance(raw, dict):
                    merged = dict(raw)
                    merged.setdefault("events", [event])
                    found.append(PolyMarket.from_payload(merged))
        return found

    def markets_by_slug_guess(self, start_ts: int) -> list[tuple[str, str, PolyMarket]]:
        """Every (slug, endpoint, market) that answers for a window at ``start_ts``.

        Tries each candidate slug shape on both the market and the event
        endpoint.  A miss is an empty answer, not an error; a network error
        propagates.
        """
        hits: list[tuple[str, str, PolyMarket]] = []
        for pattern in _SLUG_PATTERNS:
            slug = pattern.format(ts=start_ts)
            market = self.market_by_slug(slug)
            if market is not None:
                hits.append((slug, "markets", market))
            for market in self.event_by_slug(slug):
                hits.append((slug, "events", market))
        return hits

    def btc_five_minute_markets(self, limit: int = 100) -> list[PolyMarket]:
        """Every five-minute BTC up/down market in the newest ``limit``.

        Events are searched as well as markets, because a series market can
        carry its window on the event rather than on itself.
        """
        found: dict[str, PolyMarket] = {}
        for raw in self.markets(limit=limit):
            market = PolyMarket.from_payload(raw)
            if market.is_btc() and market.is_up_down() and market.is_five_minute():
                found[market.market_id or market.slug] = market
        if not found:
            for event in self.events(limit=limit):
                for raw in _json_list(event.get("markets")):
                    if not isinstance(raw, dict):
                        continue
                    merged = dict(raw)
                    merged.setdefault("events", [event])
                    market = PolyMarket.from_payload(merged)
                    if market.is_btc() and market.is_up_down() and market.is_five_minute():
                        found[market.market_id or market.slug] = market
        return sorted(found.values(), key=lambda m: m.starts_at or 0)

    def current_btc_window(self, now: int | None = None,
                           limit: int = 100) -> PolyMarket | None:
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        candidates = self.btc_five_minute_markets(limit=limit)
        open_now = [m for m in candidates if m.is_open(now)]
        if open_now:
            return open_now[-1]
        # The next window, if it opens within a minute: the watcher polls a
        # few seconds early and must not be told there is nothing.
        soon = [m for m in candidates if m.starts_at is not None
                and 0 <= m.starts_at - now <= 60]
        if soon:
            return soon[0]
        # The listing may not reach a series market at all; address the
        # current window by slug instead.
        for _, _, market in self.markets_by_slug_guess(window_start(now)):
            if market.is_btc() and (market.is_open(now) or market.window_seconds is None):
                return market
        return None

    # -- CLOB ------------------------------------------------------------ #

    def book(self, token_id: str) -> Book:
        return Book.from_payload(self._get(self.clob_url, "/book", token_id=token_id),
                                 token_id=token_id)

    def midpoint(self, token_id: str) -> float | None:
        payload = self._get(self.clob_url, "/midpoint", token_id=token_id)
        return _as_price(_first(payload, "mid", "midpoint")) if isinstance(payload, dict) else None

    def quote(self, market: PolyMarket, observed_ts: int | None = None,
              rules: VenueRules = POLYMARKET_BTC_5M) -> tuple[MarketQuote, Book, Book]:
        """The engine's quote for a market, from both sides' live books.

        A side's price is what you would pay to buy it now: its best ask.
        """
        up_token, down_token = market.token_for("Up"), market.token_for("Down")
        if up_token is None or down_token is None:
            raise PolymarketError(
                f"market {market.slug or market.market_id} has outcomes "
                f"{market.outcomes}, not Up/Down; token ids {market.token_ids}")
        if market.starts_at is None:
            raise PolymarketError(f"market {market.slug or market.market_id} has no start time")
        up_book, down_book = self.book(up_token), self.book(down_token)
        if up_book.best_ask is None or down_book.best_ask is None:
            raise PolymarketError(
                f"market {market.slug or market.market_id}: a side has no asks "
                f"(up {up_book.best_ask}, down {down_book.best_ask})")
        now = observed_ts if observed_ts is not None else int(
            datetime.now(tz=timezone.utc).timestamp())
        quote = MarketQuote(window_start_ts=market.starts_at, up_price=up_book.best_ask,
                            down_price=down_book.best_ask, observed_ts=now, rules=rules)
        return quote, up_book, down_book


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def probe(client: PolymarketClient, limit: int = 5) -> str:
    """Print what the two APIs actually return, so the guesses can be replaced."""
    lines = [f"gamma      {client.gamma_url}", f"clob       {client.clob_url}", ""]

    try:
        raw_markets = client.markets(limit=max(limit, 50))
    except PolymarketError as exc:
        return "\n".join(lines + [f"markets: {exc}"])
    lines.append(f"newest active markets returned: {len(raw_markets)}")
    if raw_markets:
        lines.append(f"keys on a market: {sorted(raw_markets[0])}")
    parsed = [PolyMarket.from_payload(r) for r in raw_markets]
    durations: dict[str, int] = {}
    for m in parsed:
        s = m.window_seconds
        key = f"{s // 60}min" if s and s % 60 == 0 else (f"{s}s" if s else "unknown")
        durations[key] = durations.get(key, 0) + 1
    lines.append(f"window lengths in this page: "
                 f"{', '.join(f'{k} x{v}' for k, v in sorted(durations.items()))}")
    btc = [m for m in parsed if m.is_btc()]
    updown = [m for m in btc if m.is_up_down()]
    five = [m for m in updown if m.is_five_minute()]
    lines.append(f"btc: {len(btc)}   btc up/down: {len(updown)}   "
                 f"five-minute btc up/down: {len(five)}")
    lines.append("")
    for m in (five or updown or btc or parsed)[:limit]:
        lines += [
            f"  id        {m.market_id}   slug {m.slug[:70]}",
            f"  question  {m.question[:80]}",
            f"  window    {m.starts_at} -> {m.ends_at} ({m.window_seconds}s)"
            f"   active {m.active} closed {m.closed} accepting {m.accepting_orders}",
            f"  outcomes  {m.outcomes}   prices {m.outcome_prices}",
            f"  tokens    {[t[:12] + '...' for t in m.token_ids]}   tick {m.tick} min {m.min_order}",
            "",
        ]

    now = int(datetime.now(tz=timezone.utc).timestamp())
    start = window_start(now)
    lines.append(f"now {now}  current window {start} -> {start + WINDOW_SECONDS}  "
                 f"slug guesses for this window and the next:")
    for ts in (start, start + WINDOW_SECONDS):
        try:
            hits = client.markets_by_slug_guess(ts)
        except PolymarketError as exc:
            lines.append(f"  {ts}: {exc}")
            continue
        if not hits:
            lines.append(f"  {ts}: no candidate slug answered "
                         f"({', '.join(p.format(ts=ts) for p in _SLUG_PATTERNS)})")
        for slug, endpoint, m in hits:
            lines.append(f"  HIT {slug} via /{endpoint}: {m.question[:60]!r} "
                         f"window {m.starts_at} -> {m.ends_at} ({m.window_seconds}s) "
                         f"outcomes {m.outcomes} tokens {len(m.token_ids)}")
            if m.is_btc() and m.is_five_minute() and m not in five:
                five.append(m)
    lines.append("")

    if not five:
        try:
            events = client.events(limit=max(limit, 50))
            lines.append(f"newest active events returned: {len(events)}")
            if events:
                lines.append(f"keys on an event: {sorted(events[0])}")
            titled = [(str(e.get('title') or e.get('slug') or '')[:70],
                       str(e.get('slug') or '')[:60],
                       len(_json_list(e.get('markets'))))
                      for e in events]
            for title, slug, n in titled[:limit * 3]:
                lines.append(f"  event  {title:<70} {slug:<60} markets {n}")
        except PolymarketError as exc:
            lines.append(f"events: {exc}")
        lines.append("")
        lines.append("No five-minute BTC up/down market matched the text filters; "
                     "read the slugs above and widen _UP_DOWN_WORDS or the BTC test.")

    target = next((m for m in five if m.is_open(now)), five[-1] if five else None)
    if target is not None and target.token_ids:
        lines.append(f"order book for {target.slug[:60]} "
                     f"({'open' if target.is_open(now) else 'not open'})")
        for name, token in zip(target.outcomes, target.token_ids):
            try:
                raw = client._get(client.clob_url, "/book", token_id=token)
                book = Book.from_payload(raw, token_id=token)
                lines.append(f"  {name:<6} bid {book.best_bid} ask {book.best_ask} "
                             f"mid {book.mid} spread {book.spread} "
                             f"ask depth {book.ask_depth():.0f}   "
                             f"book keys {sorted(raw) if isinstance(raw, dict) else type(raw).__name__}")
            except PolymarketError as exc:
                lines.append(f"  {name:<6} book: {exc}")
    if raw_markets:
        lines.append("\nRaw first matching market:")
        first = (five or updown or btc or parsed)[0].raw
        lines.append(json.dumps(first, indent=2, default=str)[:3000])
    return "\n".join(lines)
