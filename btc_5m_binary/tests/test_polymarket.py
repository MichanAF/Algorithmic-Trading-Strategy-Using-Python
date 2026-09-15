"""Polymarket read-side client. Parsing and error paths only: no network.

Fixtures are shaped like Gamma's ``/markets`` rows, which matters in one
non-obvious way: the list fields (``outcomes``, ``outcomePrices``,
``clobTokenIds``) arrive as JSON-encoded *strings*, not lists. A fixture with
tidy lists would test code the live API never exercises. Field names are still
guesses until ``btc5m probe --venue polymarket-btc-5m`` has run; what these
tests pin is that each guess, if right, is read correctly.
"""

import io
import urllib.error

import pytest

from btc5m.polymarket import (CLOB, GAMMA, WINDOW_SECONDS, Book, PolyMarket,
                              PolymarketClient, PolymarketError, _as_bool,
                              _json_list, probe, window_start)
from btc5m.venue import POLYMARKET_BTC_5M

START = 1_757_675_700                 # 2025-09-12 11:15:00 UTC = 7:15 AM ET
SLUG = f"btc-updown-5m-{START}"
UP, DOWN = "1111", "2222"             # CLOB token ids, really 77-digit integers


def market_payload(**over):
    """An open five-minute BTC up/down market as Gamma lists it."""
    base = {"id": "12345",
            "question": "Bitcoin Up or Down - September 12, 7:15AM-7:20AM ET",
            "slug": SLUG, "conditionId": "0xcond",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.52", "0.48"]',
            "clobTokenIds": f'["{UP}", "{DOWN}"]',
            "startDate": "2025-09-12T11:15:00Z", "endDate": "2025-09-12T11:20:00Z",
            "active": True, "closed": False, "acceptingOrders": True,
            "bestBid": 0.51, "bestAsk": 0.53,
            "orderPriceMinTickSize": 0.01, "orderMinSize": 5}
    base.update(over)
    return base


def book_payload(token, bids, asks, ts="1757675705123"):
    """A CLOB book: prices and sizes are strings, the timestamp is milliseconds."""
    return {"market": "0xcond", "asset_id": token, "timestamp": ts, "hash": "h",
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def books():
    """A consistent binary book: the Down side mirrors the Up side."""
    return {UP: book_payload(UP, bids=[(0.50, 40), (0.51, 120)],   # deliberately unsorted
                             asks=[(0.55, 30), (0.53, 80)]),
            DOWN: book_payload(DOWN, bids=[(0.47, 90)], asks=[(0.49, 60), (0.50, 200)])}


class Fake(PolymarketClient):
    """Answers from fixtures; records every request it was asked for."""

    def __init__(self, markets=(), events=(), books=None, slug_prefix=None,
                 reject_order=False):
        super().__init__()
        self._markets, self._events = list(markets), list(events)
        self._books = books if books is not None else {}
        self._slug_prefix = slug_prefix
        self._reject_order = reject_order
        self.calls = []

    def _get(self, base, path, **params):
        self.calls.append((base, path, params))
        if base == self.gamma_url and path in ("/markets", "/events"):
            if "slug" in params:
                slug = params["slug"]
                rows = [r for r in (self._markets if path == "/markets" else self._events)
                        if r.get("slug") == slug]
                if not rows and self._slug_prefix and slug.startswith(self._slug_prefix):
                    if path == "/markets":
                        rows = [market_payload(slug=slug)]
                    else:
                        rows = [{"slug": slug, "title": "Bitcoin Up or Down",
                                 "markets": [market_payload(slug=slug)]}]
                return rows
            if self._reject_order and "order" in params:
                raise PolymarketError(f"{base} returned 422 for {path}: bad order")
            return list(self._markets if path == "/markets" else self._events)
        if base == self.clob_url and path == "/book":
            token = params["token_id"]
            if token not in self._books:
                raise PolymarketError(f"{base} returned 404 for {path}: no book")
            return self._books[token]
        if base == self.clob_url and path == "/midpoint":
            return {"mid": "0.525"}
        raise PolymarketError(f"unexpected request {path} {params}")


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


# --------------------------------------------------------------------------- #
# markets
# --------------------------------------------------------------------------- #


def test_a_gamma_market_parses_with_its_encoded_lists():
    m = PolyMarket.from_payload(market_payload())
    assert m.market_id == "12345" and m.condition_id == "0xcond" and m.slug == SLUG
    assert m.outcomes == ["Up", "Down"]
    assert m.token_ids == [UP, DOWN]
    assert m.outcome_prices == [pytest.approx(0.52), pytest.approx(0.48)]
    assert (m.starts_at, m.ends_at) == (START, START + 300)
    assert m.window_seconds == 300 and m.is_five_minute()
    assert m.is_btc() and m.is_up_down()
    assert m.active and not m.closed and m.accepting_orders
    assert m.best_bid == pytest.approx(0.51) and m.best_ask == pytest.approx(0.53)
    assert m.tick == pytest.approx(0.01) and m.min_order == pytest.approx(0.5)  # 5 read as a price: scaled
    assert m.raw["id"] == "12345"


def test_lists_already_decoded_parse_the_same():
    m = PolyMarket.from_payload(market_payload(outcomes=["Up", "Down"],
                                               outcomePrices=[0.52, 0.48],
                                               clobTokenIds=[UP, DOWN]))
    assert m.outcomes == ["Up", "Down"] and m.token_ids == [UP, DOWN]
    assert m.outcome_prices == [pytest.approx(0.52), pytest.approx(0.48)]


def test_the_filters_reject_what_they_should():
    eth = PolyMarket.from_payload(market_payload(
        question="Ethereum Up or Down - September 12, 7:15AM-7:20AM ET",
        slug=f"eth-updown-5m-{START}"))
    assert not eth.is_btc() and eth.is_up_down() and eth.is_five_minute()
    fifteen = PolyMarket.from_payload(market_payload(endDate="2025-09-12T11:30:00Z"))
    assert fifteen.is_btc() and fifteen.window_seconds == 900 and not fifteen.is_five_minute()
    level = PolyMarket.from_payload(market_payload(
        question="Will Bitcoin hit $150k in 2025?", slug="will-bitcoin-hit-150k-in-2025",
        startDate=None, endDate="2025-12-31T23:59:00Z"))
    assert level.is_btc() and not level.is_up_down()
    assert level.window_seconds is None and not level.is_five_minute()


def test_the_window_falls_back_to_the_event_it_is_nested_in():
    row = market_payload(startDate=None, endDate=None)
    row["events"] = [{"slug": SLUG, "startDate": "2025-09-12T11:15:00Z",
                      "endDate": "2025-09-12T11:20:00Z"}]
    m = PolyMarket.from_payload(row)
    assert (m.starts_at, m.ends_at) == (START, START + 300)


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
    assert not PolyMarket.from_payload(market_payload(startDate=None)).is_open(START + 10)


# --------------------------------------------------------------------------- #
# order book
# --------------------------------------------------------------------------- #


def test_a_clob_book_parses_and_sorts_best_first():
    b = Book.from_payload(books()[UP], token_id="ignored when asset_id is present")
    assert b.token_id == UP
    assert b.bids == [(pytest.approx(0.51), 120.0), (pytest.approx(0.50), 40.0)]
    assert b.asks == [(pytest.approx(0.53), 80.0), (pytest.approx(0.55), 30.0)]
    assert b.timestamp == 1_757_675_705                 # milliseconds to seconds
    assert b.best_bid == pytest.approx(0.51) and b.best_ask == pytest.approx(0.53)
    assert b.mid == pytest.approx(0.52) and b.spread == pytest.approx(0.02)


def test_ask_depth_is_the_best_level_unless_a_limit_is_given():
    b = Book.from_payload(books()[UP])
    assert b.ask_depth() == 80.0
    assert b.ask_depth(up_to=0.55) == 110.0
    assert b.ask_depth(up_to=0.52) == 0.0


def test_an_empty_or_one_sided_book_is_safe_to_read():
    empty = Book.from_payload({"bids": [], "asks": []}, token_id=UP)
    assert empty.token_id == UP
    assert empty.best_bid is None and empty.best_ask is None
    assert empty.mid is None and empty.spread is None and empty.ask_depth() == 0.0
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
    assert [r["id"] for r in client.markets(limit=5)] == ["12345"]
    ordered, plain = client.calls
    assert ordered[2]["order"] == "startDate" and ordered[2]["ascending"] == "false"
    assert "order" not in plain[2]
    assert plain[2] == {"active": "true", "closed": "false", "limit": 5}


def test_only_five_minute_btc_up_down_markets_are_kept_in_start_order():
    later = market_payload(id="later", slug=f"btc-updown-5m-{START + 300}",
                           startDate="2025-09-12T11:20:00Z", endDate="2025-09-12T11:25:00Z")
    client = Fake(markets=[
        later,
        market_payload(id="eth", question="Ethereum Up or Down", slug="eth-updown-5m-1"),
        market_payload(id="btc15", endDate="2025-09-12T11:30:00Z"),
        market_payload(id="level", question="Will Bitcoin hit $150k?", slug="btc-150k",
                       startDate=None, endDate="2025-12-31T00:00:00Z"),
        market_payload(id="now"),
    ])
    assert [m.market_id for m in client.btc_five_minute_markets()] == ["now", "later"]


def test_the_listing_falls_back_to_events_when_no_market_matches():
    event = {"slug": SLUG, "title": "Bitcoin Up or Down",
             "startDate": "2025-09-12T11:15:00Z", "endDate": "2025-09-12T11:20:00Z",
             "markets": [market_payload(startDate=None, endDate=None)]}
    client = Fake(markets=[market_payload(id="level", question="Will Bitcoin hit $150k?",
                                          slug="btc-150k", startDate=None,
                                          endDate="2025-12-31T00:00:00Z")],
                  events=[event])
    found = client.btc_five_minute_markets()
    assert [m.market_id for m in found] == ["12345"]
    assert found[0].window_seconds == 300          # the window came from the event
    assert [c[1] for c in client.calls] == ["/markets", "/events"]


def test_the_current_window_is_the_open_one_or_the_one_about_to_open():
    past = market_payload(id="past", slug="btc-updown-5m-past",
                          startDate="2025-09-12T11:10:00Z", endDate="2025-09-12T11:15:00Z")
    nxt = market_payload(id="next", slug="btc-updown-5m-next",
                         startDate="2025-09-12T11:20:00Z", endDate="2025-09-12T11:25:00Z")
    client = Fake(markets=[past, market_payload(id="now"), nxt])
    assert client.current_btc_window(now=START + 30).market_id == "now"
    assert client.current_btc_window(now=START + 299).market_id == "now"
    assert client.current_btc_window(now=START + 300).market_id == "next"
    # Ten seconds before the window opens the next one is already the answer.
    client = Fake(markets=[past, nxt])
    assert client.current_btc_window(now=START + 290).market_id == "next"


def test_the_current_window_is_addressed_by_slug_when_the_listing_misses_it():
    client = Fake(markets=[], slug_prefix="btc-updown-5m-")
    m = client.current_btc_window(now=START + 30)
    assert m is not None and m.slug == SLUG and m.is_open(START + 30)
    guessed = [c[2]["slug"] for c in client.calls if "slug" in c[2]]
    assert guessed[0] == SLUG                       # the first guess is the one that answered
    # Nothing at all answers: None, not an exception, not a stale market.
    assert Fake(markets=[]).current_btc_window(now=START + 30) is None


def test_slug_guesses_report_which_shape_and_endpoint_answered():
    client = Fake(markets=[market_payload()], events=[
        {"slug": f"bitcoin-up-or-down-5m-{START}", "title": "Bitcoin Up or Down",
         "markets": [market_payload(id="from-event")]}])
    hits = client.markets_by_slug_guess(START)
    assert [(slug, via, m.market_id) for slug, via, m in hits] == [
        (SLUG, "markets", "12345"),
        (f"bitcoin-up-or-down-5m-{START}", "events", "from-event")]
    assert client.markets_by_slug_guess(START + 300) == []


def test_a_quote_prices_each_side_at_its_ask():
    client = Fake(books=books())
    market = PolyMarket.from_payload(market_payload())
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
        client.quote(PolyMarket.from_payload(market_payload(startDate=None)))
    thin = books()
    thin[DOWN]["asks"] = []
    with pytest.raises(PolymarketError, match="no asks"):
        Fake(books=thin).quote(PolyMarket.from_payload(market_payload()))


def test_the_midpoint_endpoint_is_read_as_a_price():
    assert Fake().midpoint(UP) == pytest.approx(0.525)


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


def test_probe_shows_a_parsed_market_and_both_books_when_the_shape_matches(monkeypatch):
    monkeypatch.setattr(PolyMarket, "is_open", lambda self, now=None: True)
    client = Fake(markets=[market_payload()], books=books())
    text = probe(client, limit=3)
    assert "newest active markets returned: 1" in text
    assert "5min x1" in text
    assert "btc: 1   btc up/down: 1   five-minute btc up/down: 1" in text
    assert f"window    {START} -> {START + 300} (300s)" in text
    assert "outcomes  ['Up', 'Down']" in text
    assert "Up     bid 0.51 ask 0.53 mid 0.52" in text
    assert "Down   bid 0.47 ask 0.49" in text
    assert "book keys ['asks', 'asset_id', 'bids', 'hash', 'market', 'timestamp']" in text
    assert "Raw first matching market" in text and '"clobTokenIds"' in text
    assert "No five-minute BTC up/down market matched" not in text


def test_probe_reports_the_slug_guesses_and_the_events_fallback():
    client = Fake(markets=[market_payload(id="level", question="Will Bitcoin hit $150k?",
                                          slug="btc-150k", startDate=None,
                                          endDate="2025-12-31T00:00:00Z")],
                  events=[{"slug": "some-event", "title": "Something else", "markets": []}])
    text = probe(client, limit=2)
    assert "five-minute btc up/down: 0" in text
    assert "no candidate slug answered (btc-updown-5m-" in text
    assert "newest active events returned: 1" in text
    assert "event  Something else" in text
    assert "widen _UP_DOWN_WORDS" in text
    assert "order book for" not in text


def test_probe_uses_a_slug_hit_when_the_listing_has_nothing():
    client = Fake(markets=[], books=books(), slug_prefix="btc-updown-5m-")
    text = probe(client)
    assert "newest active markets returned: 0" in text
    assert "HIT btc-updown-5m-" in text and "via /markets" in text
    assert "order book for btc-updown-5m-" in text
    assert "ask 0.53" in text


def test_probe_reports_an_unreachable_api_rather_than_crashing():
    class Down(PolymarketClient):
        def _get(self, base, path, **params):
            raise PolymarketError("could not reach gamma")
    text = probe(Down())
    assert text.endswith("markets: could not reach gamma")


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
