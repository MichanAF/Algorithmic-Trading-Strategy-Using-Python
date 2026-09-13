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
CRYPTO_UP_DOWN = "VariantData_CryptoUpDown"

# Candidate spellings for each value we need.  The API was unreachable when this
# was written, so rather than guess one name and fail silently, try the ones the
# docs and SDK use and report loudly when none match.
_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "marketId", "market_id", "conditionId"),
    "slug": ("slug", "ticker", "name"),
    "title": ("title", "question", "name"),
    "variant": ("marketVariant", "variant", "market_variant"),
    "status": ("status", "state"),
    "start": ("startsAt", "startTime", "eventStartTime", "openTime", "startDate"),
    "end": ("endsAt", "endTime", "closeTime", "expiryTime", "endDate"),
    "outcomes": ("outcomes", "tokens", "positions"),
    "outcome_name": ("outcome", "name", "title", "side"),
    "outcome_token": ("tokenId", "token_id", "id", "assetId"),
    "outcome_price": ("price", "lastPrice", "midPrice", "impliedProbability"),
    "fee_bps": ("feeRateBps", "fee_rate_bps", "feeBps"),
    # Needed later by the SDK's order builder, so capture them now.
    "neg_risk": ("isNegRisk", "is_neg_risk", "negRisk"),
    "yield_bearing": ("isYieldBearing", "is_yield_bearing", "yieldBearing"),
}

# The docs write the markets endpoint both ways. Try the versioned path first
# and fall back, rather than making the caller guess.
MARKET_PATHS = ("/v1/markets", "/markets")


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
class PredictMarket:
    """One predict.fun market, normalised into what the engine needs."""

    market_id: str
    title: str
    starts_at: int | None
    ends_at: int | None
    up_price: float | None
    down_price: float | None
    # The SDK's order builder needs both of these to build approvals and orders.
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None
    raw: dict = field(repr=False, default_factory=dict)

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
                f"market {self.market_id} has no start time; run `btc5m probe` "
                "and tighten the field names in predictfun._FIELDS")
        if self.up_price is None or self.down_price is None:
            raise PredictFunError(
                f"market {self.market_id} has no usable outcome prices; run "
                "`btc5m probe` to see the real payload shape")
        now = observed_ts if observed_ts is not None else int(
            datetime.now(tz=timezone.utc).timestamp())
        return MarketQuote(window_start_ts=self.starts_at, up_price=self.up_price,
                           down_price=self.down_price, observed_ts=now, rules=rules)

    @classmethod
    def from_payload(cls, payload: dict) -> "PredictMarket":
        up = down = None
        outcomes = _first(payload, "outcomes") or []
        if isinstance(outcomes, dict):
            outcomes = list(outcomes.values())
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            label = str(_first(outcome, "outcome_name") or "").strip().lower()
            price = _as_price(_first(outcome, "outcome_price"))
            if label.startswith("up") or label in ("yes", "higher"):
                up = price
            elif label.startswith("down") or label in ("no", "lower"):
                down = price
        # Two-outcome markets sum to roughly 1, so one side implies the other.
        if up is None and down is not None:
            up = 1.0 - down
        if down is None and up is not None:
            down = 1.0 - up
        def flag(key: str) -> bool | None:
            value = _first(payload, key)
            return None if value is None else bool(value)

        return cls(
            market_id=str(_first(payload, "id") or ""),
            title=str(_first(payload, "title") or _first(payload, "slug") or ""),
            starts_at=_as_epoch(_first(payload, "start")),
            ends_at=_as_epoch(_first(payload, "end")),
            up_price=up, down_price=down,
            is_neg_risk=flag("neg_risk"), is_yield_bearing=flag("yield_bearing"),
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
        """Five-minute BTC up/down markets, soonest window first."""
        found = [m for m in self.markets()
                 if m.is_five_minute() and _looks_like_btc(m.title)]
        return sorted(found, key=lambda m: m.starts_at or 0)

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
    """Print the real payload shapes, so the field guesses can be replaced.

    The API was unreachable when this module was written, so every field name in
    ``_FIELDS`` is a candidate rather than a confirmed spelling.  Run this once
    against testnet and the output says exactly which ones are right.
    """
    lines = [f"base url   {client.base_url}",
             f"api key    {'set' if client.api_key else 'none (testnet)'}", ""]
    payload = client._get_markets(marketVariant=CRYPTO_UP_DOWN, first=limit)
    items = client._items(payload)
    # Responses are wrapped: {"success": bool, "data": ..., "code"/"error"/
    # "message"/"trace" on failure}.
    lines.append(f"envelope keys: {sorted(payload) if isinstance(payload, dict) else 'list'}")
    lines.append(f"markets returned: {len(items)}")
    if not items:
        lines.append("\nNo markets came back. Try dropping the marketVariant "
                     "filter -- it is a server-side enum and the spelling may "
                     "differ from VariantData_CryptoUpDown.")
        return "\n".join(lines)

    lines.append(f"\nkeys on a market: {sorted(items[0])}")
    for raw in items[:limit]:
        market = PredictMarket.from_payload(raw)
        lines += [
            "",
            f"  id       {market.market_id or '(field name not matched)'}",
            f"  title    {market.title or '(field name not matched)'}",
            f"  window   {market.starts_at} -> {market.ends_at} "
            f"({market.window_seconds}s)",
            f"  prices   UP {market.up_price} / DOWN {market.down_price}",
        ]
        missing = [k for k, v in (("id", market.market_id), ("title", market.title),
                                  ("start", market.starts_at), ("end", market.ends_at),
                                  ("prices", market.up_price)) if not v]
        if missing:
            lines.append(f"  UNMATCHED: {missing} -- add the real key to "
                         f"predictfun._FIELDS")
    lines.append("\nRaw first market:")
    lines.append(json.dumps(items[0], indent=2)[:2000])
    return "\n".join(lines)
