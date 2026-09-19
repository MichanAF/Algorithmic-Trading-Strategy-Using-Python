"""Read-side client for the predict.fun API.

predict.fun runs the BNB Chain prediction markets that Trust Wallet's
Predictions tab and Binance Wallet both surface, so this is the venue already
in use, not a new one.

    testnet   https://api-testnet.predict.fun    no API key, 240 req/min
    mainnet   https://api.predict.fun            x-api-key header, 240 req/min

Their primary servers are in ap-northeast-1 (Tokyo).  With a 30-second entry
window that matters: run close to Tokyo and the round trip is negligible, run
from Europe or the US and it eats a real slice of the budget.

**This module only reads.**  Placing a bet means building and signing an order,
which belongs in predict.fun's own ``predict-sdk`` and in a wallet this
repository never sees.  Signing needs either an EOA private key or, for a Smart
Wallet ("Predict Account"), the account address plus the Privy wallet key --
neither of which any code here asks for or should be given.

Field names are confirmed against live testnet responses, including a real
``CRYPTO_UP_DOWN`` market.  Alternates remain in ``_FIELDS`` only where the API
genuinely varies by market kind, and the raw payload is kept on every object so
an unexpected shape can be inspected rather than guessed at.  Run
``btc5m probe`` to print what your own endpoint returns.

One thing a crypto market does **not** publish is its window: ``variantData``
carries the price feed and the settled prices, not times.  The window is derived
from the duration in ``categorySlug`` plus ``createdAt``; see ``_window``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .venue import PREDICT_FUN_BTC_5M, MarketQuote, VenueRules

MAINNET = "https://api.predict.fun"
TESTNET = "https://api-testnet.predict.fun"
CRYPTO_UP_DOWN = "CRYPTO_UP_DOWN"

# Confirmed against live responses for both market kinds.  Alternates are kept
# only where the API genuinely varies: a crypto up/down market carries no window
# times at all, while other kinds may, so those two keys stay speculative.
_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id",),
    "condition_id": ("conditionId",),
    "title": ("question", "title"),
    "variant": ("marketVariant",),
    "status": ("status",),
    "trading_status": ("tradingStatus",),
    "outcomes": ("outcomes",),
    "outcome_name": ("name",),
    "outcome_token": ("onChainId",),
    "fee_bps": ("feeRateBps",),
    "neg_risk": ("isNegRisk",),
    "yield_bearing": ("isYieldBearing",),
    "variant_data": ("variantData",),
    "category_slug": ("categorySlug",),
    "created_at": ("createdAt",),
    # Crypto markets publish no explicit window times anywhere, so these are
    # only a fallback for market kinds that do.
    "start": ("startsAt", "startTime", "openTime", "windowStartsAt"),
    "end": ("endsAt", "endTime", "closeTime", "windowEndsAt"),
}

# A crypto market's category slug encodes its duration, e.g.
# "btc-usd-up-down-2026-02-11-09-30-15-minutes".
_DURATION_RE = re.compile(r"-(\d+)-minutes?$")

MARKET_PATHS = ("/v1/markets", "/markets")

# marketVariant is a server-side enum.  Both values below are confirmed against
# live responses, so discovery has nothing left to guess -- find_variant() is
# kept only to fail loudly if the enum is ever renamed.
VARIANT_DEFAULT = "DEFAULT"
CRYPTO_VARIANT_CANDIDATES = (CRYPTO_UP_DOWN,)


class PredictFunError(RuntimeError):
    """Anything that stops us getting a usable quote."""


def _first(payload: dict, key: str) -> Any:
    for name in _FIELDS[key]:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _as_epoch(value: Any) -> int | None:
    """Accept seconds, milliseconds, or an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        while seconds > 1e11:
            seconds /= 1000.0
        return int(seconds)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def _as_price(value: Any) -> float | None:
    """Normalise a price to a 0-1 contract price.

    Venues quote these as 0.53, as 53 (percent), or as a wei-scale integer.
    Anything above 1 is scaled down rather than trusted as-is.
    """
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price > 1.0:
        while price > 1.0:
            price /= 10.0 if price < 1e3 else 1e18
    return price if 0.0 < price < 1.0 else None


@dataclass
class Outcome:
    """One side of a market, with its top of book.

    ``ask`` is what you pay to buy it, so that is the price the strategy prices
    against.  ``mid`` is the fairer read of what the market believes, and is
    what the skew veto should use -- on a wide book the two asks sum well above
    1.00 and would read as a decided market when it is only illiquid.
    """

    name: str
    token_id: str
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.ask if self.bid is None else self.bid
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @classmethod
    def from_payload(cls, payload: dict) -> "Outcome":
        def side(key: str) -> tuple[float | None, float | None]:
            book = payload.get(key)
            if not isinstance(book, dict):
                return None, None
            size = book.get("size")
            return _as_price(book.get("price")), (
                float(size) if isinstance(size, (int, float)) else None)

        bid, bid_size = side("bestBid")
        ask, ask_size = side("bestAsk")
        return cls(name=str(_first(payload, "outcome_name") or ""),
                   token_id=str(_first(payload, "outcome_token") or ""),
                   bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


@dataclass
class CryptoFeed:
    """What a crypto up/down market settles against, and its window prices.

    The feed is declared per market rather than fixed for the venue: the sample
    that confirmed this shape settles on **Pyth BTC/USD**, while the market in
    Trust Wallet's own rules text cites Chainlink BTC/USDT. Read it, never
    assume it -- the two can disagree by more than a five-minute move.
    """

    provider: str = ""
    symbol: str = ""
    feed_id: str = ""
    start_price: float | None = None
    end_price: float | None = None

    @property
    def realised_move(self) -> float | None:
        """Signed move over the window, once it has settled."""
        if self.start_price is None or self.end_price is None:
            return None
        return self.end_price - self.start_price

    @classmethod
    def from_payload(cls, payload: dict) -> "CryptoFeed":
        def number(key: str) -> float | None:
            value = payload.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        return cls(provider=str(payload.get("priceFeedProvider") or ""),
                   symbol=str(payload.get("priceFeedSymbol") or ""),
                   feed_id=str(payload.get("priceFeedId") or ""),
                   start_price=number("startPrice"), end_price=number("endPrice"))


@dataclass
class PredictMarket:
    """One predict.fun market, normalised into what the engine needs."""

    market_id: str
    title: str
    starts_at: int | None
    ends_at: int | None
    outcomes: list[Outcome] = field(default_factory=list)
    variant: str = ""
    trading_status: str = ""
    status: str = ""
    fee_rate_bps: float | None = None
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None
    condition_id: str = ""
    feed: CryptoFeed | None = None
    raw: dict = field(repr=False, default_factory=dict)

    # ------------------------------------------------------------------ #

    def outcome(self, *names: str) -> Outcome | None:
        wanted = {n.lower() for n in names}
        for out in self.outcomes:
            if out.name.strip().lower() in wanted:
                return out
        return None

    @property
    def up(self) -> Outcome | None:
        return self.outcome("up", "yes", "higher")

    @property
    def down(self) -> Outcome | None:
        return self.outcome("down", "no", "lower")

    @property
    def up_price(self) -> float | None:
        """What buying UP costs: the ask."""
        return self.up.ask if self.up else None

    @property
    def down_price(self) -> float | None:
        return self.down.ask if self.down else None

    @property
    def is_open(self) -> bool:
        return self.trading_status.upper() == "OPEN"

    @property
    def window_seconds(self) -> int | None:
        if self.starts_at is None or self.ends_at is None:
            return None
        return self.ends_at - self.starts_at

    def is_five_minute(self) -> bool:
        return self.window_seconds == 300

    def to_quote(self, observed_ts: int | None = None,
                 rules: VenueRules = PREDICT_FUN_BTC_5M) -> MarketQuote:
        """Hand the engine a quote it can price."""
        if self.starts_at is None:
            raise PredictFunError(
                f"market {self.market_id} has no derivable window. A crypto "
                "market's window comes from its category slug duration plus "
                "createdAt; this market has neither, nor explicit times.")
        up, down = self.up_price, self.down_price
        if up is None or down is None:
            named = [o.name for o in self.outcomes]
            raise PredictFunError(
                f"market {self.market_id} has no Up/Down asks to buy at; its "
                f"outcomes are {named}. Either the book is empty or the sides "
                "are named differently.")
        now = observed_ts if observed_ts is not None else int(
            datetime.now(tz=timezone.utc).timestamp())
        return MarketQuote(window_start_ts=self.starts_at, up_price=up,
                           down_price=down, observed_ts=now, rules=rules)

    @staticmethod
    def _window(payload: dict) -> tuple[int | None, int | None]:
        """Derive the trading window.

        A crypto up/down market publishes no explicit start or end anywhere:
        variantData carries prices and the feed, not times, and the human title
        gives them in ET. What is machine-readable is the category slug, which
        ends in the duration ("...-15-minutes"), and createdAt, which lands a
        few seconds inside the window it opens. Flooring createdAt to the
        duration grid recovers the boundary exactly, and stays right across
        daylight saving, which parsing ET out of the title would not.
        """
        explicit = (_as_epoch(_first(payload, "start")),
                    _as_epoch(_first(payload, "end")))
        if explicit[0] is not None and explicit[1] is not None:
            return explicit

        slug = str(_first(payload, "category_slug") or "")
        match = _DURATION_RE.search(slug)
        created = _as_epoch(_first(payload, "created_at"))
        if not match or created is None:
            return explicit

        seconds = int(match.group(1)) * 60
        if seconds <= 0:
            return explicit
        start = (created // seconds) * seconds
        return start, start + seconds

    @classmethod
    def from_payload(cls, payload: dict) -> "PredictMarket":
        variant_data = _first(payload, "variant_data")
        variant_data = variant_data if isinstance(variant_data, dict) else {}

        def flag(key: str) -> bool | None:
            value = _first(payload, key)
            return None if value is None else bool(value)

        raw_outcomes = _first(payload, "outcomes") or []
        if isinstance(raw_outcomes, dict):
            raw_outcomes = list(raw_outcomes.values())
        fee = _first(payload, "fee_bps")

        starts_at, ends_at = cls._window(payload)
        return cls(
            market_id=str(_first(payload, "id") or ""),
            title=str(_first(payload, "title") or ""),
            starts_at=starts_at, ends_at=ends_at,
            outcomes=[Outcome.from_payload(o) for o in raw_outcomes
                      if isinstance(o, dict)],
            variant=str(_first(payload, "variant") or ""),
            trading_status=str(_first(payload, "trading_status") or ""),
            status=str(_first(payload, "status") or ""),
            fee_rate_bps=float(fee) if isinstance(fee, (int, float)) else None,
            is_neg_risk=flag("neg_risk"), is_yield_bearing=flag("yield_bearing"),
            condition_id=str(_first(payload, "condition_id") or ""),
            feed=CryptoFeed.from_payload(variant_data) if variant_data else None,
            raw=payload,
        )


class PredictFunClient:
    """Minimal read-only REST client.  No keys, no signing, no orders."""

    def __init__(self, api_key: str | None = None, testnet: bool = False,
                 base_url: str | None = None, timeout: int = 15):
        self.testnet = testnet
        self.base_url = (base_url or (TESTNET if testnet else MAINNET)).rstrip("/")
        self.api_key = api_key or os.environ.get("PREDICT_FUN_API_KEY")
        self.timeout = timeout
        if not testnet and not self.api_key:
            raise PredictFunError(
                "mainnet needs an API key: pass api_key, set PREDICT_FUN_API_KEY, "
                "or use testnet=True, which needs no key at all")

    # ------------------------------------------------------------------ #

    def _get(self, path: str, **params) -> Any:
        query = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": "btc5m/1.0", "Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code in (401, 403):
                raise PredictFunError(
                    f"predict.fun rejected the credentials ({exc.code}). Check "
                    f"x-api-key, or use testnet which needs none. {detail}") from exc
            if exc.code == 429:
                raise PredictFunError(
                    "rate limited by predict.fun (the documented limit is 240 "
                    "requests a minute). Slow the polling down.") from exc
            raise PredictFunError(
                f"predict.fun returned {exc.code} for {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PredictFunError(
                f"could not reach {self.base_url} ({exc}). Corporate and sandbox "
                "network policy often blocks this; run it from a machine with "
                "direct access.") from exc

    @staticmethod
    def _items(payload: Any) -> list[dict]:
        """Pull the list out of whatever envelope the API wraps it in."""
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("data", "markets", "items", "results", "edges", "nodes"):
                inner = payload.get(key)
                if isinstance(inner, list):
                    # A GraphQL-style edge list wraps each item in {"node": {...}}.
                    return [x.get("node", x) if isinstance(x, dict) else {}
                            for x in inner]
                if isinstance(inner, dict):
                    return PredictFunClient._items(inner)
        return []

    # ------------------------------------------------------------------ #

    def markets(self, variant: str | None = CRYPTO_UP_DOWN,
                status: str | None = None, first: int = 50,
                after: str | None = None) -> list[PredictMarket]:
        """List markets.

        ``status`` defaults to None on purpose: it is a server-side enum and
        "ACTIVE" is not one of its values -- testnet rejects that with a 400
        naming MarketStatusFilter. Omit it and filter locally until the valid
        values are confirmed.
        """
        payload = self._get_markets(marketVariant=variant, status=status,
                                    first=first, after=after)
        return [PredictMarket.from_payload(item) for item in self._items(payload)]

    def _get_markets(self, **params) -> Any:
        """Try each documented spelling of the markets path."""
        last: PredictFunError | None = None
        for path in MARKET_PATHS:
            try:
                return self._get(path, **params)
            except PredictFunError as exc:
                if "404" not in str(exc):
                    raise
                last = exc
        raise last or PredictFunError("no markets endpoint responded")

    def orderbook(self, market_id: str) -> dict:
        return self._get(f"/v1/orderbook/{market_id}")

    def btc_five_minute_markets(self) -> list[PredictMarket]:
        """Open five-minute BTC up/down markets, soonest window first."""
        found = [m for m in self.markets()
                 if m.is_open and m.is_five_minute() and _looks_like_btc(m.title)]
        return sorted(found, key=lambda m: m.starts_at or 0)

    def find_variant(self) -> str | None:
        """Which marketVariant value the crypto up/down markets use.

        The enum is server-side and only "DEFAULT" is confirmed, so try the
        plausible spellings and return the first the API accepts. A wrong value
        comes back as a 400, not an empty list.
        """
        for candidate in CRYPTO_VARIANT_CANDIDATES:
            try:
                payload = self._get_markets(marketVariant=candidate, first=1)
            except PredictFunError as exc:
                if "400" in str(exc):
                    continue
                raise
            if self._items(payload):
                return candidate
        return None

    def current_btc_window(self, now: int | None = None) -> PredictMarket | None:
        """The five-minute window that is open right now, if any."""
        now = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
        for market in self.btc_five_minute_markets():
            if market.starts_at is not None and market.ends_at is not None:
                if market.starts_at <= now < market.ends_at:
                    return market
        return None


def _looks_like_btc(title: str) -> bool:
    lowered = title.lower()
    return "btc" in lowered or "bitcoin" in lowered


def probe(client: PredictFunClient, limit: int = 3) -> str:
    """Print what the API actually returns, so the guesses can be replaced."""
    lines = [f"base url   {client.base_url}",
             f"api key    {'set' if client.api_key else 'none (testnet)'}", ""]

    variant = client.find_variant()
    if variant:
        lines.append(f"crypto marketVariant: {variant}")
        payload = client._get_markets(marketVariant=variant, first=limit)
    else:
        lines.append(f"crypto marketVariant: none of "
                     f"{list(CRYPTO_VARIANT_CANDIDATES)} was accepted -- "
                     f"listing unfiltered instead")
        payload = client._get_markets(first=limit)

    items = client._items(payload)
    envelope = sorted(payload) if isinstance(payload, dict) else "list"
    lines += [f"envelope keys: {envelope}", f"markets returned: {len(items)}"]
    if not items:
        lines.append("\nNo markets came back.")
        return "\n".join(lines)

    lines.append(f"\nkeys on a market: {sorted(items[0])}")
    for raw in items[:limit]:
        market = PredictMarket.from_payload(raw)
        lines += [
            "",
            f"  id        {market.market_id}",
            f"  title     {market.title[:60]}",
            f"  variant   {market.variant}   status {market.status}"
            f"   trading {market.trading_status}",
            f"  fee       {market.fee_rate_bps} bps",
            f"  window    {market.starts_at} -> {market.ends_at} "
            f"({market.window_seconds}s)   5min? {market.is_five_minute()}",
        ]
        if market.feed:
            lines.append(
                f"  feed      {market.feed.provider} {market.feed.symbol}"
                f"   start {market.feed.start_price} end {market.feed.end_price}")
        for out in market.outcomes:
            lines.append(f"  outcome   {out.name:<10} bid {out.bid} "
                         f"ask {out.ask} mid {out.mid}")
        if market.starts_at is None:
            vd = raw.get("variantData")
            lines.append(f"  NO WINDOW TIMES. categorySlug = "
                         f"{_first(raw, 'category_slug')!r}, variantData = "
                         f"{json.dumps(vd)[:300] if vd else 'null'}")
            lines.append("  -> a crypto window comes from the slug duration plus "
                         "createdAt; add explicit keys to predictfun._FIELDS")
    # predict.fun lists 5- and 15-minute BTC windows side by side, and only the
    # 300-second ones are this strategy's bet.  Summarise what came back so a
    # live run can confirm the 5-minute markets are visible at all before
    # wondering why btc_five_minute_markets() returned nothing.
    parsed = [PredictMarket.from_payload(raw) for raw in items]
    durations: dict[str, int] = {}
    for market in parsed:
        seconds = market.window_seconds
        key = f"{seconds // 60}min" if seconds else "unknown"
        durations[key] = durations.get(key, 0) + 1
    lines.append(f"\nwindow lengths in this page: "
                 f"{', '.join(f'{k} x{v}' for k, v in sorted(durations.items()))}")
    btc5m = [m for m in parsed if m.is_five_minute() and _looks_like_btc(m.title)]
    lines.append(f"five-minute BTC markets: {len(btc5m)} "
                 f"({sum(1 for m in btc5m if m.is_open)} open)")

    lines.append("\nRaw first market:")
    lines.append(json.dumps(items[0], indent=2)[:2500])
    return "\n".join(lines)
