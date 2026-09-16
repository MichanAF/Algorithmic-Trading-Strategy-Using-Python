"""Polymarket, read side only: the public Gamma and CLOB APIs.

Two hosts, no keys, no signing, no orders:

    gamma-api.polymarket.com   market and event metadata (what exists, when it
                               opens and closes, which two token ids trade)
    clob.polymarket.com        the order book for a token id (what a share
                               costs right now), and the market's own record

Both answer unauthenticated GETs.  Nothing in this module can move money: the
CLOB's order endpoints need a signed API key and a funded wallet, and neither
is anywhere in this repository.

What the live API confirmed, from ``btc5m probe --venue polymarket-btc-5m``
run on 2026-09-15 (the workflow ``polymarket-probe.yml`` keeps the log):

* A five-minute BTC window is one market in one event, both at the slug
  ``btc-updown-5m-<start>`` where ``<start>`` is the window's opening second
  as a unix timestamp.  ``/markets?slug=`` and ``/events?slug=`` both answer.
* ``endDate`` is the window's end.  ``startDate`` is **not** its start: it is
  when the market went live, about a day earlier.  The start comes from the
  slug, and the window length from the slug's ``5m``.
* ``outcomes``, ``outcomePrices`` and ``clobTokenIds`` are JSON-encoded
  *strings*; every list here is read through a decoder that accepts either
  form.  Outcomes are ``Up`` and ``Down``, in that order.
* ``eventStartTime`` on the market (``startTime`` on its event) is the
  window's opening second, and it equals the slug's timestamp.
* ``orderMinSize`` is 5 (shares), ``orderPriceMinTickSize`` 0.01.
* Resolution: Up if the Chainlink BTC/USD 60-second TWAP at the end of the
  window is greater than or equal to the price at its start.  A tie pays Up.
* The fee is the market's ``feeSchedule``: ``{rate: 0.07, exponent: 1,
  takerOnly: true, rebateRate: 0.2}`` under ``feeType: crypto_fees_v2``, and
  the documentation gives ``fee = C x feeRate x p x (1 - p)`` with makers
  never charged.  ``makerBaseFee`` / ``takerBaseFee`` (1000) and the CLOB's
  ``/fee-rate`` (``base_fee`` 1000) are base fields the schedule overrides.
* Gamma's ``outcomePrices``, ``bestBid``, ``bestAsk`` and ``lastTradePrice``
  were minutes stale on an open window (``updatedAt`` before the window
  opened) while the CLOB book had moved from 0.51 to 0.07.  Prices come from
  the book, never from Gamma.
"""

from __future__ import annotations

import json
import re
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
WINDOW_SECONDS = 300

# The words a BTC up/down market's question or slug carries.
_UP_DOWN_WORDS = ("up or down", "up-or-down", "updown", "up/down")

# A timed window's slug: asset, length in minutes, opening second.  Confirmed
# live: btc-updown-5m-1789481100 is "Bitcoin Up or Down - September 15,
# 10:05AM-10:10AM ET", and 1789481100 is 14:05:00 UTC that day.
_SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]+)-updown-(?P<minutes>\d+)m-(?P<ts>\d{9,11})$")


class PolymarketError(RuntimeError):
    pass


def window_start(now: int, seconds: int = WINDOW_SECONDS) -> int:
    """The start of the window containing ``now``: five-minute boundaries."""
    return now - now % seconds


def slug_for_window(start_ts: int, asset: str = "btc", minutes: int = 5) -> str:
    """The slug Polymarket gives the ``minutes``-minute ``asset`` window at ``start_ts``."""
    return f"{asset}-updown-{minutes}m-{start_ts}"


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


def _as_float(value: Any) -> float | None:
    """A plain number (a size, a fee rate, a delay), not a price."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_opt_bool(value: Any) -> bool | None:
    return None if value is None else _as_bool(value)


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
    starts_at: int | None                  # the window's opening second (from the slug)
    ends_at: int | None                    # the window's end (endDate)
    active: bool
    closed: bool
    accepting_orders: bool
    asset: str = ""                        # from the slug: "btc", "eth", ...
    window_minutes: int | None = None      # from the slug: 5, 15, 60, ...
    listed_at: int | None = None           # Gamma's startDate: when it went live
    created_at: int | None = None
    resolution_source: str = ""
    best_bid: float | None = None          # Gamma's own view of outcome 0's book
    best_ask: float | None = None
    tick: float | None = None
    min_order: float | None = None         # shares, not a price
    maker_base_fee: float | None = None    # as sent; units confirmed on the runner
    taker_base_fee: float | None = None
    fees_enabled: bool | None = None
    fee_schedule: Any = None
    seconds_delay: float | None = None
    clear_book_on_start: bool | None = None
    updated_at: int | None = None          # when Gamma last refreshed its prices
    series_slug: str = ""                  # "btc-up-or-down-5m"
    fee_type: str = ""                     # "crypto_fees_v2"
    fee_rate: float | None = None          # feeSchedule.rate: 0.07 on crypto
    fee_exponent: float = 1.0
    fee_taker_only: bool = True
    fee_rebate_rate: float | None = None   # share of taker fees rebated to makers
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict) -> "PolyMarket":
        outcomes = [str(o) for o in _json_list(_first(payload, "outcomes"))]
        prices = [_as_price(p) for p in _json_list(_first(payload, "outcomePrices"))]
        tokens = [str(t) for t in _json_list(_first(payload, "clobTokenIds",
                                                     "clob_token_ids", "tokenIds"))]
        slug = str(_first(payload, "slug", "marketSlug") or "")
        # A market nested in an event may carry its dates on the event.
        events = _first(payload, "events") or []
        event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else {}
        listed = _as_epoch(_first(payload, "startDate", "start_date"))
        if listed is None:
            listed = _as_epoch(_first(event, "startDate", "start_date"))
        ends = _as_epoch(_first(payload, "endDate", "end_date"))
        if ends is None:
            ends = _as_epoch(_first(event, "endDate", "end_date"))

        timed = _SLUG_RE.match(slug)
        asset, minutes = "", None
        # The declared opening second first, the slug's second, then a game
        # start.  Never the listing time.
        starts = _as_epoch(_first(payload, "eventStartTime", "event_start_time"))
        if starts is None:
            starts = _as_epoch(_first(event, "startTime", "start_time"))
        if timed:
            asset, minutes = timed["asset"], int(timed["minutes"])
            if starts is None:
                starts = int(timed["ts"])
            if ends is None:
                ends = starts + minutes * 60
        elif starts is None:
            starts = _as_epoch(_first(payload, "gameStartTime", "game_start_time"))
        schedule = _first(payload, "feeSchedule")
        schedule = schedule if isinstance(schedule, dict) else {}
        series = _first(event, "seriesSlug") or _first(payload, "seriesSlug") or ""

        return cls(
            market_id=str(_first(payload, "id", "marketId") or ""),
            question=str(_first(payload, "question", "title") or ""),
            slug=slug,
            condition_id=str(_first(payload, "conditionId", "condition_id") or ""),
            outcomes=outcomes, token_ids=tokens, outcome_prices=prices,
            starts_at=starts, ends_at=ends,
            active=_as_bool(_first(payload, "active")),
            closed=_as_bool(_first(payload, "closed")),
            accepting_orders=_as_bool(_first(payload, "acceptingOrders",
                                             "accepting_orders")),
            asset=asset, window_minutes=minutes,
            listed_at=listed,
            created_at=_as_epoch(_first(payload, "createdAt", "created_at")),
            resolution_source=str(_first(payload, "resolutionSource") or ""),
            best_bid=_as_price(_first(payload, "bestBid", "best_bid")),
            best_ask=_as_price(_first(payload, "bestAsk", "best_ask")),
            tick=_as_float(_first(payload, "orderPriceMinTickSize")),
            min_order=_as_float(_first(payload, "orderMinSize")),
            maker_base_fee=_as_float(_first(payload, "makerBaseFee")),
            taker_base_fee=_as_float(_first(payload, "takerBaseFee")),
            fees_enabled=_as_opt_bool(_first(payload, "feesEnabled")),
            fee_schedule=_first(payload, "feeSchedule"),
            seconds_delay=_as_float(_first(payload, "secondsDelay")),
            clear_book_on_start=_as_opt_bool(_first(payload, "clearBookOnStart")),
            updated_at=_as_epoch(_first(payload, "updatedAt", "updated_at")),
            series_slug=str(series),
            fee_type=str(_first(payload, "feeType") or ""),
            fee_rate=_as_float(schedule.get("rate")),
            fee_exponent=_as_float(schedule.get("exponent")) or 1.0,
            fee_taker_only=_as_bool(schedule.get("takerOnly", True)),
            fee_rebate_rate=_as_float(schedule.get("rebateRate")),
            raw=payload,
        )

    def fee_per_share(self, price: float, taker: bool = True) -> float:
        """USDC per share at ``price``, from the market's own schedule.

        ``fee = rate x (p x (1 - p)) ** exponent``, the documented formula at
        exponent 1.  Makers pay nothing when the schedule is taker-only.  A
        market with no schedule is priced at the venue profile's rate.
        """
        if not taker and self.fee_taker_only:
            return 0.0
        rate = self.fee_rate if self.fee_rate is not None else POLYMARKET_BTC_5M.fee_coefficient
        return rate * (price * (1.0 - price)) ** self.fee_exponent

    def prices_age(self, now: int | None = None) -> int | None:
        """Seconds since Gamma last refreshed its prices; None if unknown."""
        if self.updated_at is None:
            return None
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        return now - self.updated_at

    @property
    def window_seconds(self) -> int | None:
        if self.starts_at is None or self.ends_at is None:
            return None
        return self.ends_at - self.starts_at

    def is_five_minute(self) -> bool:
        return self.window_seconds == WINDOW_SECONDS

    def is_up_down(self) -> bool:
        text = f"{self.question} {self.slug}".lower()
        return any(w in text for w in _UP_DOWN_WORDS)

    def is_btc(self) -> bool:
        if self.asset:
            return self.asset == "btc"
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

    def settled_side(self) -> str | None:
        """'Up' or 'Down' once Gamma has moved the prices to 1 and 0; else None."""
        if not self.closed or len(self.outcomes) != len(self.outcome_prices):
            return None
        winners = [name for name, price in zip(self.outcomes, self.outcome_prices)
                   if price is not None and price >= 0.999]
        # _as_price reads an exact 1 as "not a price"; Gamma sends "1" and "0".
        if not winners:
            raw = _json_list(_first(self.raw, "outcomePrices"))
            winners = [name for name, price in zip(self.outcomes, raw)
                       if _as_float(price) is not None and _as_float(price) >= 0.999]
        return winners[0] if len(winners) == 1 else None


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
                size = _as_float(_first(row, "size"))
                if price is not None:
                    out.append((price, size if size is not None else 0.0))
            return out
        bids = sorted(levels(_first(payload, "bids")), key=lambda x: -x[0])
        asks = sorted(levels(_first(payload, "asks")), key=lambda x: x[0])
        ts = _as_float(_first(payload, "timestamp"))
        ts_int = int(ts) if ts is not None else None
        if ts_int is not None and ts_int > 1e11:          # milliseconds
            ts_int //= 1000
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

    def bid_depth(self, down_to: float | None = None) -> float:
        """Shares bid at the best bid, or at or above ``down_to``."""
        if not self.bids:
            return 0.0
        limit = self.bids[0][0] if down_to is None else down_to
        return sum(size for price, size in self.bids if price >= limit - 1e-12)


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
        """The markets nested in the event at ``slug``, with the event's dates."""
        found = []
        for event in self._items(self._get(self.gamma_url, "/events", slug=slug)):
            for raw in _json_list(event.get("markets")):
                if isinstance(raw, dict):
                    merged = dict(raw)
                    merged.setdefault("events", [event])
                    found.append(PolyMarket.from_payload(merged))
        return found

    def market_for_window(self, start_ts: int, asset: str = "btc",
                          minutes: int = 5) -> PolyMarket | None:
        """The market for the window opening at ``start_ts``, by its slug.

        The market endpoint first, the event endpoint if that is empty.  A
        window nobody has listed is None; a network error propagates.
        """
        slug = slug_for_window(start_ts, asset, minutes)
        market = self.market_by_slug(slug)
        if market is not None:
            return market
        nested = self.event_by_slug(slug)
        return nested[0] if nested else None

    def btc_five_minute_markets(self, limit: int = 100) -> list[PolyMarket]:
        """Every five-minute BTC up/down market in the newest ``limit``.

        The listing is ordered by listing time, and windows are listed about a
        day ahead, so this finds tomorrow's windows more readily than the one
        open now.  ``market_for_window`` is the way to the current window.
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

    def current_btc_window(self, now: int | None = None) -> PolyMarket | None:
        """The five-minute BTC market whose window contains ``now``."""
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        market = self.market_for_window(window_start(now))
        if market is not None:
            return market
        # The slug shape may change one day; the listing is the slow road.
        open_now = [m for m in self.btc_five_minute_markets() if m.is_open(now)]
        return open_now[-1] if open_now else None

    def next_btc_window(self, now: int | None = None) -> PolyMarket | None:
        """The window after the one containing ``now``; the watcher polls early."""
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        return self.market_for_window(window_start(now) + WINDOW_SECONDS)

    # -- CLOB ------------------------------------------------------------ #

    def book(self, token_id: str) -> Book:
        return Book.from_payload(self._get(self.clob_url, "/book", token_id=token_id),
                                 token_id=token_id)

    def midpoint(self, token_id: str) -> float | None:
        payload = self._get(self.clob_url, "/midpoint", token_id=token_id)
        return _as_price(_first(payload, "mid", "midpoint")) if isinstance(payload, dict) else None

    def clob_market(self, condition_id: str) -> dict:
        """The CLOB's own record of a market: fees, delay, tick, tokens."""
        payload = self._get(self.clob_url, f"/markets/{condition_id}")
        return payload if isinstance(payload, dict) else {}

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


def _describe(m: PolyMarket) -> list[str]:
    return [
        f"  id        {m.market_id}   slug {m.slug[:70]}   condition {m.condition_id[:18]}...",
        f"  question  {m.question[:80]}",
        f"  window    {m.starts_at} -> {m.ends_at} ({m.window_seconds}s)   "
        f"listed {m.listed_at}   created {m.created_at}",
        f"  flags     active {m.active} closed {m.closed} accepting {m.accepting_orders}   "
        f"secondsDelay {m.seconds_delay}   clearBookOnStart {m.clear_book_on_start}",
        f"  outcomes  {m.outcomes}   prices {m.outcome_prices}   settled {m.settled_side()}",
        f"  tokens    {[t[:12] + '...' for t in m.token_ids]}   tick {m.tick} min order {m.min_order}",
        f"  fees      schedule rate {m.fee_rate} exponent {m.fee_exponent} taker only "
        f"{m.fee_taker_only} rebate {m.fee_rebate_rate} type {m.fee_type!r}   "
        f"per share at 0.50: {m.fee_per_share(0.5):.4f}   "
        f"(base fields: maker {m.maker_base_fee} taker {m.taker_base_fee} enabled {m.fees_enabled})",
        f"  gamma     prices as of {m.updated_at} ({m.prices_age()}s ago): "
        f"bid {m.best_bid} ask {m.best_ask} -- stale on an open window, read the book",
        f"  resolves  {m.resolution_source}   series {m.series_slug}",
    ]


def probe(client: PolymarketClient, limit: int = 5) -> str:
    """Print what the two APIs actually return, so the guesses can be replaced."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    start = window_start(now)
    lines = [f"gamma      {client.gamma_url}", f"clob       {client.clob_url}",
             f"now        {now}   current window {start} -> {start + WINDOW_SECONDS}   "
             f"slug {slug_for_window(start)}", ""]

    # 1. The current window, addressed by slug.
    current = None
    try:
        current = client.market_for_window(start)
    except PolymarketError as exc:
        return "\n".join(lines + [f"current window: {exc}"])
    if current is None:
        lines.append("current window: nothing at that slug on /markets or /events")
    else:
        lines.append(f"current window ({'open' if current.is_open(now) else 'NOT open'}):")
        lines += _describe(current)
        for name, token in zip(current.outcomes, current.token_ids):
            try:
                raw = client._get(client.clob_url, "/book", token_id=token)
                book = Book.from_payload(raw, token_id=token)
                lines.append(f"  {name:<6} bid {book.best_bid} ask {book.best_ask} "
                             f"mid {book.mid} spread {book.spread} "
                             f"bid depth {book.bid_depth():.0f} ask depth {book.ask_depth():.0f}   "
                             f"book keys {sorted(raw) if isinstance(raw, dict) else type(raw).__name__}")
            except PolymarketError as exc:
                lines.append(f"  {name:<6} book: {exc}")
        clob_raw: dict = {}
        if current.condition_id:
            try:
                clob_raw = client.clob_market(current.condition_id)
                picked = {k: clob_raw.get(k) for k in (
                    "maker_base_fee", "taker_base_fee", "fee_rate_bps", "seconds_delay",
                    "game_start_time", "end_date_iso", "minimum_order_size",
                    "minimum_tick_size", "accepting_orders", "accepting_order_timestamp",
                    "neg_risk", "is_50_50_outcome", "enable_order_book") if k in clob_raw}
                lines.append(f"  clob      keys {sorted(clob_raw)}")
                lines.append(f"  clob      {json.dumps(picked, default=str)}")
                tokens = clob_raw.get("tokens")
                if isinstance(tokens, list):
                    lines.append(f"  clob      tokens {json.dumps(tokens, default=str)[:400]}")
            except PolymarketError as exc:
                lines.append(f"  clob      market: {exc}")
        if current.token_ids:
            try:
                fee = client._get(client.clob_url, "/fee-rate", token_id=current.token_ids[0])
                lines.append(f"  fee-rate  {json.dumps(fee, default=str)[:300]}")
            except PolymarketError as exc:
                lines.append(f"  fee-rate  {str(exc)[:200]}")
    lines.append("")

    # 2. The neighbours: is the next one listed, how does a settled one look.
    for label, ts in (("next", start + WINDOW_SECONDS), ("previous", start - WINDOW_SECONDS),
                      ("an hour ago", start - 12 * WINDOW_SECONDS)):
        try:
            m = client.market_for_window(ts)
        except PolymarketError as exc:
            lines.append(f"{label} window {ts}: {exc}")
            continue
        if m is None:
            lines.append(f"{label} window {ts}: not listed")
        else:
            lines.append(f"{label} window {ts}: {m.slug}   closed {m.closed} accepting "
                         f"{m.accepting_orders}   prices {m.outcome_prices}   settled "
                         f"{m.settled_side()}   uma {json.dumps(m.raw.get('umaResolutionStatuses'), default=str)[:120]}")
    lines.append("")

    # 3. The listing, for the record: what else is there and how it is named.
    try:
        raw_markets = client.markets(limit=max(limit, 50))
    except PolymarketError as exc:
        raw_markets = []
        lines.append(f"markets listing: {exc}")
    if raw_markets:
        parsed = [PolyMarket.from_payload(r) for r in raw_markets]
        timed: dict[str, int] = {}
        for m in parsed:
            if m.window_minutes is not None:
                key = f"{m.asset}-{m.window_minutes}m"
                timed[key] = timed.get(key, 0) + 1
        lines.append(f"newest active markets returned: {len(raw_markets)}   "
                     f"timed slugs: {', '.join(f'{k} x{v}' for k, v in sorted(timed.items())) or 'none'}")
        five = [m for m in parsed if m.is_btc() and m.is_five_minute()]
        lines.append(f"five-minute btc markets in the listing: {len(five)}"
                     + (f"   e.g. {five[0].slug} window {five[0].starts_at} -> {five[0].ends_at}"
                        if five else ""))
        if current is None and not five:
            lines.append("No five-minute BTC market anywhere; the slug shape may have changed. "
                         "Read the slugs below and update _SLUG_RE.")
            for m in parsed[:limit * 3]:
                lines.append(f"  {m.slug[:60]:<60} {m.question[:60]}")
    lines.append("")

    # 4. Raw payloads, cut short.
    subject = current
    if subject is None and raw_markets:
        subject = next((PolyMarket.from_payload(r) for r in raw_markets), None)
    if subject is not None:
        lines.append("Raw Gamma market:")
        lines.append(json.dumps(subject.raw, indent=1, default=str)[:6000])
        if current is not None and current.condition_id:
            try:
                lines.append("\nRaw CLOB market:")
                lines.append(json.dumps(client.clob_market(current.condition_id),
                                        indent=1, default=str)[:3000])
            except PolymarketError as exc:
                lines.append(f"clob market: {exc}")
    return "\n".join(lines)
