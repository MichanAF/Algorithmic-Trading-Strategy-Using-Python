"""Read-side client for the predict.fun API.

predict.fun runs the BNB Chain prediction markets that Trust Wallet's
Predictions tab and Binance Wallet both surface, so this is the venue already
in use, not a new one.

    testnet   https://api-testnet.predict.fun    no API key, 240 req/min
    mainnet   https://api.predict.fun            x-api-key header, 240 req/min

Their primary servers are in ap-northeast-1 (Tokyo).  With a 45-second entry
window that matters: run close to Tokyo and the round trip is negligible, run
from Europe or the US and it eats a real slice of the budget.

**This module only reads.**  Placing a bet means building and signing an order,
which belongs in predict.fun's own ``predict-sdk`` and in a wallet this
repository never sees.  Signing needs either an EOA private key or, for a Smart
Wallet ("Predict Account"), the account address plus the Privy wallet key --
neither of which any code here asks for or should be given.

Field names are handled defensively on purpose.  The API could not be reached
from the environment this was written in, so every accessor tries the plausible
spellings and the raw payload is kept on the object.  Run ``btc5m probe`` against
testnet to print the real shapes, then tighten ``_FIELDS`` to match.
"""

from __future__ import annotations

import json
import os
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

# Candidate spellings for each value we need.  The API was unreachable when this
# was written, so rather than guess one name and fail silently, try the ones the
# docs and SDK use and report loudly when none match.
# Confirmed against a live testnet response.  Alternates are kept only where the
# API genuinely varies (timing lives in variantData and differs by market kind).
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
    # Timing: absent on a DEFAULT market, expected inside variantData on a
    # crypto up/down one.  Both levels are searched.
    "start": ("startsAt", "startTime", "openTime", "windowStartsAt", "startDate"),
    "end": ("endsAt", "endTime", "closeTime", "windowEndsAt", "endDate"),
}

MARKET_PATHS = ("/v1/markets", "/markets")

# marketVariant is a server-side enum.  "DEFAULT" is confirmed; the crypto
# up/down spelling is not.  VariantData_CryptoUpDown names the *shape* of the
# variantData object, not the enum value, so it is a poor guess for the filter.
VARIANT_DEFAULT = "DEFAULT"
CRYPTO_VARIANT_CANDIDATES = ("CRYPTO_UP_DOWN", "CRYPTO_UPDOWN", "CRYPTO")


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
                f"market {self.market_id} has no window start. On a crypto "
                "up/down market the timing lives in variantData; run "
                "`btc5m probe` against one and add the real keys to _FIELDS.")
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

    @classmethod
    def from_payload(cls, payload: dict) -> "PredictMarket":
        variant_data = _first(payload, "variant_data")
        variant_data = variant_data if isinstance(variant_data, dict) else {}

        def timing(key: str) -> int | None:
            # Timing sits on the market for some kinds and inside variantData
            # for others, so look in both.
            return _as_epoch(_first(payload, key) or _first(variant_data, key))

        def flag(key: str) -> bool | None:
            value = _first(payload, key)
            return None if value is None else bool(value)

        raw_outcomes = _first(payload, "outcomes") or []
        if isinstance(raw_outcomes, dict):
            raw_outcomes = list(raw_outcomes.values())
        fee = _first(payload, "fee_bps")

        return cls(
            market_id=str(_first(payload, "id") or ""),
            title=str(_first(payload, "title") or ""),
            starts_at=timing("start"), ends_at=timing("end"),
            outcomes=[Outcome.from_payload(o) for o in raw_outcomes
                      if isinstance(o, dict)],
            variant=str(_first(payload, "variant") or ""),
            trading_status=str(_first(payload, "trading_status") or ""),
            status=str(_first(payload, "status") or ""),
            fee_rate_bps=float(fee) if isinstance(fee, (int, float)) else None,
            is_neg_risk=flag("neg_risk"), is_yield_bearing=flag("yield_bearing"),
            condition_id=str(_first(payload, "condition_id") or ""),
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
            f"({market.window_seconds}s)",
        ]
        for out in market.outcomes:
            lines.append(f"  outcome   {out.name:<10} bid {out.bid} "
                         f"ask {out.ask} mid {out.mid}")
        if market.starts_at is None:
            vd = raw.get("variantData")
            lines.append(f"  NO WINDOW TIMES. variantData = "
                         f"{json.dumps(vd)[:300] if vd else 'null'}")
            lines.append("  -> add its timing keys to predictfun._FIELDS")
    lines.append("\nRaw first market:")
    lines.append(json.dumps(items[0], indent=2)[:2500])
    return "\n".join(lines)
