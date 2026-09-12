"""predict.fun read-side client. Parsing and error paths only: no network."""

import pytest

from btc5m.predictfun import (CRYPTO_UP_DOWN, MAINNET, TESTNET, PredictFunClient,
                              PredictFunError, PredictMarket, _as_epoch, _as_price,
                              _looks_like_btc)

WINDOW = 1_757_675_700


def payload(**over):
    base = {"id": "m1", "title": "Bitcoin Up or Down - 5m",
            "startsAt": WINDOW, "endsAt": WINDOW + 300,
            "outcomes": [{"outcome": "Up", "tokenId": "t1", "price": 0.49},
                         {"outcome": "Down", "tokenId": "t2", "price": 0.51}]}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# normalisers
# --------------------------------------------------------------------------- #

def test_price_accepts_the_shapes_a_venue_might_send():
    assert _as_price(0.53) == pytest.approx(0.53)
    assert _as_price("0.53") == pytest.approx(0.53)
    assert _as_price(53) == pytest.approx(0.53)       # percent
    assert _as_price(None) is None
    assert _as_price("nonsense") is None
    assert _as_price(0.0) is None                     # not a tradable price
    assert _as_price(1.0) is None


def test_epoch_accepts_seconds_milliseconds_and_iso():
    assert _as_epoch(WINDOW) == WINDOW
    assert _as_epoch(WINDOW * 1000) == WINDOW
    assert _as_epoch("2026-09-12T12:15:00Z") == 1_789_215_300
    assert _as_epoch(None) is None
    assert _as_epoch("not a date") is None


def test_btc_titles_are_recognised():
    assert _looks_like_btc("Bitcoin Up or Down")
    assert _looks_like_btc("BTC 5m")
    assert not _looks_like_btc("Ethereum Up or Down")


# --------------------------------------------------------------------------- #
# market parsing
# --------------------------------------------------------------------------- #

def test_a_well_formed_market_parses():
    m = PredictMarket.from_payload(payload())
    assert m.market_id == "m1"
    assert m.window_seconds == 300
    assert m.is_five_minute()
    assert m.up_price == pytest.approx(0.49)
    assert m.down_price == pytest.approx(0.51)


def test_one_side_implies_the_other():
    """Two-outcome markets sum to about 1, so a missing side is recoverable."""
    m = PredictMarket.from_payload(payload(
        outcomes=[{"outcome": "Down", "price": 0.6}]))
    assert m.down_price == pytest.approx(0.6)
    assert m.up_price == pytest.approx(0.4)


def test_alternative_field_spellings_are_tolerated():
    """Field names are guesses until `btc5m probe` confirms them."""
    m = PredictMarket.from_payload({
        "marketId": "m2", "question": "BTC Up or Down",
        "startTime": WINDOW * 1000, "endTime": (WINDOW + 300) * 1000,
        "tokens": [{"name": "Up", "lastPrice": 45},
                   {"name": "Down", "lastPrice": 55}]})
    assert m.market_id == "m2"
    assert m.is_five_minute()
    assert m.up_price == pytest.approx(0.45)


def test_a_non_five_minute_window_is_not_mistaken_for_one():
    hourly = PredictMarket.from_payload(payload(endsAt=WINDOW + 3600))
    assert hourly.window_seconds == 3600
    assert not hourly.is_five_minute()


def test_an_unparseable_market_says_so_rather_than_guessing():
    m = PredictMarket.from_payload({"totally": "unexpected"})
    assert m.starts_at is None
    assert m.up_price is None
    with pytest.raises(PredictFunError, match="no start time"):
        m.to_quote()


def test_a_market_without_prices_is_refused():
    m = PredictMarket.from_payload(payload(outcomes=[]))
    with pytest.raises(PredictFunError, match="no usable outcome prices"):
        m.to_quote()


def test_to_quote_hands_the_engine_something_it_can_price():
    m = PredictMarket.from_payload(payload())
    q = m.to_quote(observed_ts=WINDOW + 6)
    assert q.window_start_ts == WINDOW
    assert q.seconds_into_window == 6
    assert q.seconds_to_expiry == 294
    assert q.up_price == pytest.approx(0.49)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

def test_mainnet_without_a_key_is_refused_with_the_remedy():
    with pytest.raises(PredictFunError, match="needs an API key"):
        PredictFunClient()


def test_testnet_needs_no_key():
    client = PredictFunClient(testnet=True)
    assert client.base_url == TESTNET
    assert client.api_key is None


def test_an_env_var_key_is_picked_up(monkeypatch):
    monkeypatch.setenv("PREDICT_FUN_API_KEY", "secret")
    client = PredictFunClient()
    assert client.base_url == MAINNET
    assert client.api_key == "secret"


def test_envelopes_are_unwrapped():
    """The list may arrive bare, under data/markets/items, or as GraphQL edges."""
    items = [{"id": "a"}, {"id": "b"}]
    for envelope in (items,
                     {"data": items},
                     {"markets": items},
                     {"results": items},
                     {"data": {"items": items}},
                     {"edges": [{"node": {"id": "a"}}, {"node": {"id": "b"}}]}):
        assert [x["id"] for x in PredictFunClient._items(envelope)] == ["a", "b"]


def test_an_unrecognised_envelope_yields_nothing_rather_than_raising():
    assert PredictFunClient._items({"unexpected": 1}) == []
    assert PredictFunClient._items("nonsense") == []


def test_the_crypto_up_down_variant_is_the_documented_one():
    assert CRYPTO_UP_DOWN == "VariantData_CryptoUpDown"


def test_current_window_picks_the_open_one(monkeypatch):
    client = PredictFunClient(testnet=True)
    markets = [
        PredictMarket.from_payload(payload(id="past", startsAt=WINDOW - 600,
                                           endsAt=WINDOW - 300)),
        PredictMarket.from_payload(payload(id="now")),
        PredictMarket.from_payload(payload(id="next", startsAt=WINDOW + 300,
                                           endsAt=WINDOW + 600)),
    ]
    monkeypatch.setattr(client, "btc_five_minute_markets", lambda: markets)
    assert client.current_btc_window(now=WINDOW + 30).market_id == "now"
    assert client.current_btc_window(now=WINDOW - 1000) is None


def test_only_five_minute_btc_markets_are_kept(monkeypatch):
    client = PredictFunClient(testnet=True)
    monkeypatch.setattr(client, "markets", lambda **kw: [
        PredictMarket.from_payload(payload(id="btc5m")),
        PredictMarket.from_payload(payload(id="eth5m", title="Ethereum Up or Down")),
        PredictMarket.from_payload(payload(id="btc1h", endsAt=WINDOW + 3600)),
    ])
    assert [m.market_id for m in client.btc_five_minute_markets()] == ["btc5m"]


def test_probe_reports_unmatched_fields_instead_of_failing(monkeypatch):
    from btc5m.predictfun import probe

    client = PredictFunClient(testnet=True)
    monkeypatch.setattr(client, "_get",
                        lambda *a, **kw: {"data": [{"totally": "unexpected"}]})
    text = probe(client)
    assert "UNMATCHED" in text
    assert "_FIELDS" in text


def test_probe_shows_a_parsed_market_when_the_fields_do_match(monkeypatch):
    from btc5m.predictfun import probe

    client = PredictFunClient(testnet=True)
    monkeypatch.setattr(client, "_get", lambda *a, **kw: {"data": [payload()]})
    text = probe(client)
    assert "UNMATCHED" not in text
    assert "300s" in text
    assert "UP 0.49" in text
