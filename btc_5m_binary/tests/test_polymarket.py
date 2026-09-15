"""Polymarket read-side client. Parsing and error paths only: no network.

Fixtures are shaped like the live Gamma ``/markets`` row the probe printed on
2026-09-15, which matters in three non-obvious ways: the list fields
(``outcomes``, ``outcomePrices``, ``clobTokenIds``) are JSON-encoded *strings*;
``startDate`` is when the market went live, about a day before its window, so
the window's start has to come from the slug; and ``orderMinSize`` is a share
count, not a price. A fixture with tidy lists and a truthful ``startDate``
would test code the live API never exercises.
"""

import io
import urllib.error

import pytest

from btc5m.polymarket import (CLOB, GAMMA, WINDOW_SECONDS, Book, PolyMarket,
                              PolymarketClient, PolymarketError, _as_bool,
                              _json_list, probe, slug_for_window, window_start)
from btc5m.venue import POLYMARKET_BTC_5M

START = 1_789_481_100                 # 2026-09-15 14:05:00 UTC = 10:05 AM ET
SLUG = f"btc-updown-5m-{START}"
LISTED = "2026-09-14T14:13:43.368992Z"   # went live a day before the window
UP = "48725203892934312879943960580039737032377183552237448650809765374081985392830"
DOWN = "53386981535633980194149663070564313041423339357501069430757741675992317482769"
COND = "0x4cd09109b1cb594a514c6a8544920b894a72778108fe90d055c502cefc19e5ad"


def market_payload(**over):
    """A five-minute BTC up/down market as Gamma lists it, live shape."""
    base = {"id": "4552589",
            "question": "Bitcoin Up or Down - September 15, 10:05AM-10:10AM ET",
            "slug": SLUG, "conditionId": COND,
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.505", "0.495"]',
            "clobTokenIds": f'["{UP}", "{DOWN}"]',
            "startDate": LISTED, "endDate": "2026-09-15T14:10:00Z",
            "eventStartTime": "2026-09-15T14:05:00Z",
            "createdAt": "2026-09-14T14:12:39.553788Z",
            "updatedAt": "2026-09-15T14:03:58.622168Z",   # before the window opened
            "active": True, "closed": False, "acceptingOrders": True,
            "bestBid": 0.5, "bestAsk": 0.51, "lastTradePrice": 0.51, "spread": 0.01,
            "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
            "makerBaseFee": 1000, "takerBaseFee": 1000, "feesEnabled": True,
            "feeType": "crypto_fees_v2",
            "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
            "secondsDelay": 0, "clearBookOnStart": False,
            "negRisk": False, "restricted": True}
    base.update(over)
    return base


def event_payload(market, **over):
    base = {"id": "1022968", "ticker": market["slug"], "slug": market["slug"],
            "title": market["question"], "startDate": market.get("startDate"),
            "startTime": market.get("eventStartTime"), "endDate": market.get("endDate"),
            "seriesSlug": "btc-up-or-down-5m", "active": True, "closed": False,
            "markets": [market]}
    base.update(over)
    return base


def book_payload(token, bids, asks, ts="1789481105123"):
    """A CLOB book: prices and sizes are strings, the timestamp is milliseconds."""
    return {"market": COND, "asset_id": token, "timestamp": ts, "hash": "h",
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def books():
    """A consistent binary book: the Down side mirrors the Up side."""
    return {UP: book_payload(UP, bids=[(0.50, 40), (0.51, 120)],   # deliberately unsorted
                             asks=[(0.55, 30), (0.53, 80)]),
            DOWN: book_payload(DOWN, bids=[(0.47, 90)], asks=[(0.49, 60), (0.50, 200)])}


def clob_market_payload(**over):
    base = {"condition_id": COND, "question_id": "0xq", "market_slug": SLUG,
            "end_date_iso": "2026-09-15T14:10:00Z", "game_start_time": None,
            "seconds_delay": 3, "maker_base_fee": 1000, "taker_base_fee": 1000,
            "minimum_order_size": 5, "minimum_tick_size": 0.01,
            "accepting_orders": True, "neg_risk": False, "is_50_50_outcome": False,
            "enable_order_book": True,
            "tokens": [{"token_id": UP, "outcome": "Up", "price": 0.53, "winner": False},
                       {"token_id": DOWN, "outcome": "Down", "price": 0.47, "winner": False}]}
    base.update(over)
    return base


class Fake(PolymarketClient):
    """Answers from fixtures; records every request it was asked for."""

    def __init__(self, markets=(), events=(), books=None, clob=None,
                 reject_order=False, fee_rate=None):
        super().__init__()
        self._markets, self._events = list(markets), list(events)
        self._books = books if books is not None else {}
        self._clob = clob if clob is not None else {}
        self._reject_order = reject_order
        self._fee_rate = fee_rate
        self.calls = []

    def _get(self, base, path, **params):
        self.calls.append((base, path, params))
        if base == self.gamma_url and path in ("/markets", "/events"):
            rows = self._markets if path == "/markets" else self._events
            if "slug" in params:
                return [r for r in rows if r.get("slug") == params["slug"]]
            if self._reject_order and "order" in params:
                raise PolymarketError(f"{base} returned 422 for {path}: bad order")
            return list(rows)
        if base == self.clob_url and path == "/book":
            token = params["token_id"]
            if token not in self._books:
                raise PolymarketError(f"{base} returned 404 for {path}: no book")
            return self._books[token]
        if base == self.clob_url and path == "/midpoint":
            return {"mid": "0.525"}
        if base == self.clob_url and path.startswith("/markets/"):
            cid = path.split("/")[-1]
            if cid not in self._clob:
                raise PolymarketError(f"{base} returned 404 for {path}: no market")
            return self._clob[cid]
        if base == self.clob_url and path == "/fee-rate":
            if self._fee_rate is None:
                raise PolymarketError(f"{base} returned 404 for {path}: not found")
            return self._fee_rate
        raise PolymarketError(f"unexpected request {path} {params}")


def fake_with_window(**over):
    """A client whose /markets?slug= answers for the fixture window."""
    row = market_payload(**over)
    return Fake(markets=[row], books=books(), clob={COND: clob_market_payload()})


# --------------------------------------------------------------------------- #
# normalisers
# --------------------------------------------------------------------------- #


def test_lists_are_read_whether_gamma_sends_them_encoded_or_not():
    assert _json_list(["Up", "Down"]) == ["Up", "Down"]
    assert _json_list('["Up", "Down"]') == ["Up", "Down"]
    assert _json_list('["0.52", "0.48"]') == ["0.52", "0.48"]
    assert _json_list("Up") == ["Up"]              # a bare scalar is one item
    assert _json_list("") == []
    assert _json_list(None) == []
    assert _json_list('["broken') == []             # malformed JSON is nothing, not a crash
    assert _json_list('{"a": 1}') == []             # an object is not a list
    assert _json_list(42) == []


def test_booleans_arrive_as_strings_too():
    assert _as_bool(True) is True
    assert _as_bool("true") is True and _as_bool("True") is True
    assert _as_bool("false") is False and _as_bool("") is False
    assert _as_bool(None) is False and _as_bool(1) is True


def test_window_start_floors_to_the_five_minute_boundary():
    assert window_start(START) == START
    assert window_start(START + 299) == START
    assert window_start(START + 300) == START + WINDOW_SECONDS
    assert WINDOW_SECONDS == POLYMARKET_BTC_5M.window_seconds


def test_the_slug_is_asset_length_and_opening_second():
    assert slug_for_window(START) == "btc-updown-5m-1789481100"
    assert slug_for_window(START, asset="eth", minutes=15) == "eth-updown-15m-1789481100"


# --------------------------------------------------------------------------- #
# markets
# --------------------------------------------------------------------------- #


def test_a_gamma_market_parses_with_its_encoded_lists():
    m = PolyMarket.from_payload(market_payload())
    assert m.market_id == "4552589" and m.condition_id == COND and m.slug == SLUG
    assert m.outcomes == ["Up", "Down"]
    assert m.token_ids == [UP, DOWN]
    assert m.outcome_prices == [pytest.approx(0.505), pytest.approx(0.495)]
    assert m.active and not m.closed and m.accepting_orders
    assert m.best_ask == pytest.approx(0.51) and m.best_bid == pytest.approx(0.5)
    assert m.tick == pytest.approx(0.01)
    assert m.min_order == 5.0                       # shares, not scaled as a price
    assert m.maker_base_fee == 1000.0 and m.taker_base_fee == 1000.0
    assert m.fees_enabled is True and m.fee_schedule["rate"] == 0.07
    assert m.seconds_delay == 0.0 and m.clear_book_on_start is False
    assert m.resolution_source.endswith("btc-usd-twap-60s-streams")
    assert m.updated_at == START - 62
    assert m.raw["id"] == "4552589"


def test_the_declared_start_wins_and_the_slug_is_the_fallback():
    """eventStartTime is the window's opening second; the slug agrees on the
    live market, and stands in when the field is absent."""
    declared = PolyMarket.from_payload(market_payload(eventStartTime="2026-09-15T14:06:00Z"))
    assert declared.starts_at == START + 60          # the field, even against the slug
    from_slug = PolyMarket.from_payload(market_payload(eventStartTime=None))
    assert from_slug.starts_at == START
    nested = market_payload(eventStartTime=None, endDate=None)
    event = event_payload(nested, startTime="2026-09-15T14:05:00Z")
    nested["events"] = [event]
    from_event = PolyMarket.from_payload(nested)
    assert (from_event.starts_at, from_event.ends_at) == (START, START + 300)
    assert from_event.series_slug == "btc-up-or-down-5m"


def test_the_fee_comes_from_the_market_schedule_and_matches_the_documentation():
    """fee = C x rate x p x (1 - p), crypto rate 0.07, makers never charged:
    1.75 cents a share at 0.50, which is what the venue profile charges."""
    m = PolyMarket.from_payload(market_payload())
    assert m.fee_type == "crypto_fees_v2"
    assert m.fee_rate == 0.07 and m.fee_exponent == 1.0
    assert m.fee_taker_only is True and m.fee_rebate_rate == 0.2
    assert m.fee_per_share(0.50) == pytest.approx(0.0175)
    assert m.fee_per_share(0.30) == pytest.approx(0.0147)
    assert m.fee_per_share(0.30) == pytest.approx(m.fee_per_share(0.70))
    assert m.fee_per_share(0.50, taker=False) == 0.0
    assert m.fee_per_share(0.50) == pytest.approx(POLYMARKET_BTC_5M.fee_per_share(0.50))
    # No schedule on the payload: the profile's rate, not zero.
    bare = PolyMarket.from_payload(market_payload(feeSchedule=None))
    assert bare.fee_rate is None and bare.fee_per_share(0.5) == pytest.approx(0.0175)
    # A schedule that charges makers too is honoured, not assumed away.
    both = PolyMarket.from_payload(market_payload(
        feeSchedule={"rate": 0.05, "exponent": 1, "takerOnly": False}))
    assert both.fee_per_share(0.5, taker=False) == pytest.approx(0.0125)


def test_gamma_prices_carry_their_age():
    m = PolyMarket.from_payload(market_payload())
    assert m.prices_age(now=START + 197) == 259
    assert PolyMarket.from_payload(market_payload(updatedAt=None)).prices_age(now=START) is None


def test_the_window_comes_from_the_slug_and_end_date_not_from_start_date():
    """Gamma's startDate is the listing time, a day early. The slug carries the
    opening second and endDate the close; startDate is kept only as listed_at."""
    m = PolyMarket.from_payload(market_payload())
    assert (m.starts_at, m.ends_at) == (START, START + 300)
    assert m.window_seconds == 300 and m.is_five_minute()
    assert m.asset == "btc" and m.window_minutes == 5
    assert m.listed_at == START - 86_400 + 523      # 2026-09-14 14:13:43 UTC
    assert m.created_at == START - 86_400 + 459


def test_a_missing_end_date_is_filled_from_the_slug_length():
    m = PolyMarket.from_payload(market_payload(endDate=None))
    assert (m.starts_at, m.ends_at) == (START, START + 300)
    fifteen = PolyMarket.from_payload(market_payload(slug=f"btc-updown-15m-{START}",
                                                     endDate=None))
    assert fifteen.window_seconds == 900 and not fifteen.is_five_minute()


def test_a_market_without_a_timed_slug_has_no_window_unless_it_declares_a_start():
    hourly = PolyMarket.from_payload(market_payload(
        slug="bitcoin-up-or-down-september-17-2026-10am-et",
        question="Bitcoin Up or Down - September 17, 10AM ET",
        endDate="2026-09-17T15:00:00Z", eventStartTime=None))
    assert hourly.starts_at is None and hourly.ends_at == 1_789_657_200
    assert hourly.window_seconds is None and not hourly.is_five_minute()
    assert hourly.is_btc() and hourly.is_up_down()          # by its words
    declared = PolyMarket.from_payload(market_payload(
        slug="some-game", eventStartTime=None, gameStartTime="2026-09-15T14:05:00Z"))
    assert declared.starts_at == START and declared.window_seconds == 300


def test_lists_already_decoded_parse_the_same():
    m = PolyMarket.from_payload(market_payload(outcomes=["Up", "Down"],
                                               outcomePrices=[0.505, 0.495],
                                               clobTokenIds=[UP, DOWN]))
    assert m.outcomes == ["Up", "Down"] and m.token_ids == [UP, DOWN]
    assert m.outcome_prices == [pytest.approx(0.505), pytest.approx(0.495)]


def test_the_filters_reject_what_they_should():
    eth = PolyMarket.from_payload(market_payload(
        question="Ethereum Up or Down - September 15, 10:05AM-10:10AM ET",
        slug=f"eth-updown-5m-{START}"))
    assert not eth.is_btc() and eth.is_up_down() and eth.is_five_minute()
    fifteen = PolyMarket.from_payload(market_payload(slug=f"btc-updown-15m-{START}",
                                                     endDate="2026-09-15T14:20:00Z"))
    assert fifteen.is_btc() and fifteen.window_seconds == 900 and not fifteen.is_five_minute()
    level = PolyMarket.from_payload(market_payload(
        question="Will Bitcoin hit $150k in 2025?", slug="will-bitcoin-hit-150k-in-2025",
        endDate="2025-12-31T23:59:00Z", eventStartTime=None))
    assert level.is_btc() and not level.is_up_down()
    assert level.window_seconds is None and not level.is_five_minute()


def test_the_end_falls_back_to_the_event_it_is_nested_in():
    row = market_payload(endDate=None, slug="odd-slug", eventStartTime=None)
    row["events"] = [{"slug": SLUG, "startDate": LISTED, "endDate": "2026-09-15T14:10:00Z"}]
    m = PolyMarket.from_payload(row)
    assert m.ends_at == START + 300 and m.listed_at == START - 86_400 + 523
    assert m.starts_at is None                      # no timed slug, no declared start


def test_tokens_are_matched_to_sides_by_name():
    m = PolyMarket.from_payload(market_payload())
    assert m.token_for("Up") == UP and m.token_for("down") == DOWN
    assert m.token_for("Yes") is None
    yes_no = PolyMarket.from_payload(market_payload(outcomes='["Yes", "No"]'))
    assert yes_no.token_for("Up") is None


def test_open_means_inside_the_window_and_not_closed():
    m = PolyMarket.from_payload(market_payload())
    assert m.is_open(START) and m.is_open(START + 299)
    assert not m.is_open(START - 1) and not m.is_open(START + 300)
    assert not PolyMarket.from_payload(market_payload(closed=True)).is_open(START + 10)
    assert not PolyMarket.from_payload(market_payload(slug="untimed",
                                                      eventStartTime=None)).is_open(START + 10)


def test_a_settled_market_names_its_winner_from_the_one_and_zero():
    """Gamma moves outcomePrices to "1" and "0" once resolved; an exact 1 is not
    a tradable price, so the winner is read from the raw strings."""
    down = PolyMarket.from_payload(market_payload(closed=True, outcomePrices='["0", "1"]'))
    assert down.settled_side() == "Down"
    up = PolyMarket.from_payload(market_payload(closed=True, outcomePrices='["1", "0"]'))
    assert up.settled_side() == "Up"
    live = PolyMarket.from_payload(market_payload())
    assert live.settled_side() is None
    unclear = PolyMarket.from_payload(market_payload(closed=True, outcomePrices='["0.5", "0.5"]'))
    assert unclear.settled_side() is None


# --------------------------------------------------------------------------- #
# order book
# --------------------------------------------------------------------------- #


def test_a_clob_book_parses_and_sorts_best_first():
    b = Book.from_payload(books()[UP], token_id="ignored when asset_id is present")
    assert b.token_id == UP
    assert b.bids == [(pytest.approx(0.51), 120.0), (pytest.approx(0.50), 40.0)]
    assert b.asks == [(pytest.approx(0.53), 80.0), (pytest.approx(0.55), 30.0)]
    assert b.timestamp == 1_789_481_105                 # milliseconds to seconds
    assert b.best_bid == pytest.approx(0.51) and b.best_ask == pytest.approx(0.53)
    assert b.mid == pytest.approx(0.52) and b.spread == pytest.approx(0.02)


def test_depth_is_the_best_level_unless_a_limit_is_given():
    b = Book.from_payload(books()[UP])
    assert b.ask_depth() == 80.0
    assert b.ask_depth(up_to=0.55) == 110.0
    assert b.ask_depth(up_to=0.52) == 0.0
    assert b.bid_depth() == 120.0
    assert b.bid_depth(down_to=0.50) == 160.0


def test_an_empty_or_one_sided_book_is_safe_to_read():
    empty = Book.from_payload({"bids": [], "asks": []}, token_id=UP)
    assert empty.token_id == UP
    assert empty.best_bid is None and empty.best_ask is None
    assert empty.mid is None and empty.spread is None
    assert empty.ask_depth() == 0.0 and empty.bid_depth() == 0.0
    one_sided = Book.from_payload(book_payload(UP, bids=[(0.5, 10)], asks=[]))
    assert one_sided.best_bid == pytest.approx(0.5) and one_sided.best_ask is None
    assert one_sided.mid is None


def test_book_rows_with_junk_are_skipped_not_fatal():
    b = Book.from_payload({"bids": [{"price": "x", "size": "1"}, {"price": "0.4", "size": "n"},
                                    "not a row"],
                           "asks": None, "timestamp": "soon"}, token_id=UP)
    assert b.bids == [(pytest.approx(0.4), 0.0)]
    assert b.asks == [] and b.timestamp is None


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


def test_envelopes_are_unwrapped():
    items = PolymarketClient._items
    assert items([{"a": 1}, "junk", {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert items({"data": [{"a": 1}]}) == [{"a": 1}]
    assert items({"markets": [{"a": 1}]}) == [{"a": 1}]
    assert items({"events": [{"a": 1}]}) == [{"a": 1}]
    assert items({"id": "one market"}) == [{"id": "one market"}]
    assert items({}) == [] and items(None) == [] and items("text") == []


def test_the_market_listing_falls_back_when_gamma_rejects_the_ordering():
    client = Fake(markets=[market_payload()], reject_order=True)
    assert [r["id"] for r in client.markets(limit=5)] == ["4552589"]
    ordered, plain = client.calls
    assert ordered[2]["order"] == "startDate" and ordered[2]["ascending"] == "false"
    assert "order" not in plain[2]
    assert plain[2] == {"active": "true", "closed": "false", "limit": 5}


def test_a_window_is_found_by_slug_on_the_market_endpoint_first():
    client = fake_with_window()
    m = client.market_for_window(START)
    assert m is not None and m.slug == SLUG and m.starts_at == START
    assert [c[1] for c in client.calls] == ["/markets"]
    assert client.calls[0][2] == {"slug": SLUG}


def test_a_window_only_the_event_endpoint_knows_is_still_found():
    nested = market_payload(endDate=None)
    client = Fake(events=[event_payload(nested)])
    m = client.market_for_window(START)
    assert m is not None and (m.starts_at, m.ends_at) == (START, START + 300)
    assert [c[1] for c in client.calls] == ["/markets", "/events"]
    assert client.calls[1][2] == {"slug": SLUG}


def test_a_window_nobody_listed_is_none_not_an_error():
    client = fake_with_window()
    assert client.market_for_window(START + 300) is None
    assert client.market_for_window(START, asset="eth") is None


def test_the_current_window_is_addressed_by_its_boundary():
    client = fake_with_window()
    assert client.current_btc_window(now=START).slug == SLUG
    assert client.current_btc_window(now=START + 299).slug == SLUG
    assert client.current_btc_window(now=START + 300) is None     # next one not listed
    assert client.next_btc_window(now=START - 1).slug == SLUG


def test_the_current_window_falls_back_to_the_listing_when_the_slug_misses():
    """If the slug shape changes, a market that is open now and reads as a
    five-minute BTC window is still found through the listing."""
    row = market_payload(slug=f"btc-updown-5m-{START}")
    client = Fake(markets=[row])
    client.market_for_window = lambda ts, asset="btc", minutes=5: None
    assert client.current_btc_window(now=START + 30).market_id == "4552589"
    assert Fake(markets=[]).current_btc_window(now=START + 30) is None


def test_only_five_minute_btc_up_down_markets_are_kept_in_start_order():
    later = market_payload(id="later", slug=f"btc-updown-5m-{START + 300}",
                           eventStartTime="2026-09-15T14:10:00Z", endDate="2026-09-15T14:15:00Z")
    client = Fake(markets=[
        later,
        market_payload(id="eth", question="Ethereum Up or Down", slug=f"eth-updown-5m-{START}"),
        market_payload(id="btc15", slug=f"btc-updown-15m-{START}",
                       endDate="2026-09-15T14:20:00Z"),
        market_payload(id="level", question="Will Bitcoin hit $150k?", slug="btc-150k",
                       endDate="2025-12-31T00:00:00Z", eventStartTime=None),
        market_payload(id="now"),
    ])
    assert [m.market_id for m in client.btc_five_minute_markets()] == ["now", "later"]


def test_the_listing_falls_back_to_events_when_no_market_matches():
    nested = market_payload(endDate=None)
    client = Fake(markets=[market_payload(id="level", question="Will Bitcoin hit $150k?",
                                          slug="btc-150k", endDate="2025-12-31T00:00:00Z")],
                  events=[event_payload(nested)])
    found = client.btc_five_minute_markets()
    assert [m.market_id for m in found] == ["4552589"]
    assert found[0].window_seconds == 300          # the end came from the event
    assert [c[1] for c in client.calls] == ["/markets", "/events"]


def test_a_quote_prices_each_side_at_its_ask():
    client = fake_with_window()
    market = client.market_for_window(START)
    quote, up, down = client.quote(market, observed_ts=START + 5)
    assert quote.window_start_ts == START and quote.observed_ts == START + 5
    assert quote.up_price == pytest.approx(0.53) and quote.down_price == pytest.approx(0.49)
    assert quote.rules is POLYMARKET_BTC_5M
    assert up.token_id == UP and down.token_id == DOWN
    assert up.ask_depth() == 80.0 and down.ask_depth() == 60.0


def test_a_quote_refuses_a_market_it_cannot_price():
    client = Fake(books=books())
    with pytest.raises(PolymarketError, match="not Up/Down"):
        client.quote(PolyMarket.from_payload(market_payload(outcomes='["Yes", "No"]')))
    with pytest.raises(PolymarketError, match="no start time"):
        client.quote(PolyMarket.from_payload(market_payload(slug="untimed", eventStartTime=None)))
    thin = books()
    thin[DOWN]["asks"] = []
    with pytest.raises(PolymarketError, match="no asks"):
        Fake(books=thin).quote(PolyMarket.from_payload(market_payload()))


def test_the_clob_market_record_and_midpoint_are_read():
    client = fake_with_window()
    record = client.clob_market(COND)
    assert record["taker_base_fee"] == 1000 and record["tokens"][0]["outcome"] == "Up"
    assert client.midpoint(UP) == pytest.approx(0.525)
    with pytest.raises(PolymarketError, match="404"):
        client.clob_market("0xnothing")


def test_http_errors_name_the_host_and_code(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {},
                                     io.BytesIO(b'{"error":"no such market"}'))
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(PolymarketError, match=r"returned 404 for /book: .*no such market"):
        PolymarketClient().book(UP)


def test_an_unreachable_host_says_so_with_the_remedy(monkeypatch):
    def blocked(request, timeout):
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
    monkeypatch.setattr("urllib.request.urlopen", blocked)
    with pytest.raises(PolymarketError, match="could not reach .*direct access"):
        PolymarketClient().markets()


def test_requests_carry_the_query_and_no_key(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        return Response(b"[]")
    monkeypatch.setattr("urllib.request.urlopen", capture)
    assert PolymarketClient().market_by_slug(SLUG) is None
    assert seen["url"] == f"{GAMMA}/markets?slug={SLUG}"
    assert not any("auth" in k.lower() or "key" in k.lower() for k in seen["headers"])
    assert PolymarketClient().gamma_url == GAMMA and PolymarketClient().clob_url == CLOB


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def test_probe_shows_the_current_window_its_books_and_the_clob_record(monkeypatch):
    monkeypatch.setattr("btc5m.polymarket.window_start", lambda now, seconds=300: START)
    client = fake_with_window()
    text = probe(client, limit=3)
    assert f"slug {SLUG}" in text
    assert "current window (" in text and f"window    {START} -> {START + 300} (300s)" in text
    assert "min order 5.0" in text
    assert "fees      schedule rate 0.07 exponent 1.0 taker only True rebate 0.2" in text
    assert "per share at 0.50: 0.0175" in text and "maker 1000.0 taker 1000.0" in text
    assert "secondsDelay 0.0   clearBookOnStart False" in text
    assert "gamma     prices as of" in text and "read the book" in text
    assert "series btc-up-or-down-5m" not in text        # /markets alone carries no event
    assert "Up     bid 0.51 ask 0.53 mid 0.52" in text
    assert "Down   bid 0.47 ask 0.49" in text
    assert "book keys ['asks', 'asset_id', 'bids', 'hash', 'market', 'timestamp']" in text
    assert '"taker_base_fee": 1000' in text and '"outcome": "Up"' in text
    assert "fee-rate  " in text and "404" in text            # the endpoint guess, reported
    assert f"next window {START + 300}: not listed" in text
    assert "Raw Gamma market:" in text and '"clobTokenIds"' in text
    assert "Raw CLOB market:" in text


def test_probe_reports_a_settled_neighbour():
    settled = market_payload(id="prev", slug=f"btc-updown-5m-{START - 300}",
                             endDate="2026-09-15T14:05:00Z", closed=True,
                             outcomePrices='["0", "1"]', umaResolutionStatuses='["resolved"]')
    client = Fake(markets=[market_payload(), settled], books=books(),
                  clob={COND: clob_market_payload()})
    import btc5m.polymarket as pm
    real = pm.window_start
    pm.window_start = lambda now, seconds=300: START
    try:
        text = probe(client)
    finally:
        pm.window_start = real
    assert f"previous window {START - 300}: btc-updown-5m-{START - 300}   closed True" in text
    assert "settled Down" in text and 'uma "[\\"resolved\\"]"' in text


def test_probe_says_when_nothing_answers_and_shows_the_listing(monkeypatch):
    monkeypatch.setattr("btc5m.polymarket.window_start", lambda now, seconds=300: START)
    client = Fake(markets=[market_payload(id="level", question="Will Bitcoin hit $150k?",
                                          slug="btc-150k", endDate="2025-12-31T00:00:00Z"),
                           market_payload(id="sol", slug=f"sol-updown-5m-{START + 86_400}")])
    text = probe(client, limit=2)
    assert "current window: nothing at that slug" in text
    assert "timed slugs: sol-5m x1" in text
    assert "five-minute btc markets in the listing: 0" in text
    assert "update _SLUG_RE" in text
    assert "  btc-150k" in text
    assert "Raw Gamma market:" in text and "Raw CLOB market:" not in text


def test_probe_reports_an_unreachable_api_rather_than_crashing():
    class Down(PolymarketClient):
        def _get(self, base, path, **params):
            raise PolymarketError("could not reach gamma")
    text = probe(Down())
    assert text.endswith("current window: could not reach gamma")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_the_probe_command_routes_by_venue(monkeypatch, capsys):
    from btc5m.cli import main
    import btc5m.cli as cli

    monkeypatch.setattr(cli, "probe_polymarket",
                        lambda client, limit: f"POLY {type(client).__name__} {limit}")
    monkeypatch.setattr(cli, "probe_predictfun",
                        lambda client, limit: f"PREDICT {limit}")
    assert main(["probe", "--venue", "polymarket-btc-5m", "--limit", "7"]) == 0
    assert capsys.readouterr().out.strip() == "POLY PolymarketClient 7"
    assert main(["probe", "--testnet"]) == 0
    assert capsys.readouterr().out.strip() == "PREDICT 3"
