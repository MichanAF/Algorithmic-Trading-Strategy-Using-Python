"""predict.fun read-side client. Parsing and error paths only: no network."""

import pytest

from btc5m.predictfun import (CRYPTO_UP_DOWN, CRYPTO_VARIANT_CANDIDATES, MAINNET,
                              TESTNET, Outcome, PredictFunClient, PredictFunError,
                              PredictMarket, _as_epoch, _as_price, _looks_like_btc)

WINDOW = 1_757_675_700


def book(bid, ask):
    return {"bestBid": {"price": bid, "size": 100.0},
            "bestAsk": {"price": ask, "size": 100.0}}


def payload(**over):
    """Shaped like a real testnet response, with a crypto up/down variantData."""
    base = {"id": 1, "question": "Bitcoin Up or Down - 5m",
            "conditionId": "0xabc", "marketVariant": "CRYPTO_UP_DOWN",
            "status": "REGISTERED", "tradingStatus": "OPEN",
            "feeRateBps": 200, "isNegRisk": False, "isYieldBearing": False,
            "variantData": {"startsAt": WINDOW, "endsAt": WINDOW + 300},
            # A consistent binary book: the Down side mirrors the Up side, so
            # the two asks sum above 1.00 by exactly the spread.
            "outcomes": [{"name": "Up", "onChainId": "t1", **book(0.47, 0.49)},
                         {"name": "Down", "onChainId": "t2", **book(0.51, 0.53)}]}
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
    assert m.market_id == "1"
    assert m.title.startswith("Bitcoin")
    assert m.variant == "CRYPTO_UP_DOWN"
    assert m.trading_status == "OPEN"
    assert m.is_open
    assert m.fee_rate_bps == pytest.approx(200.0)
    assert m.window_seconds == 300
    assert m.is_five_minute()


def test_the_price_to_buy_is_the_ask_not_the_mid():
    """You pay the ask. Pricing against the mid quietly overstates the edge."""
    m = PredictMarket.from_payload(payload())
    assert m.up_price == pytest.approx(0.49)
    assert m.down_price == pytest.approx(0.53)
    assert m.up.mid == pytest.approx(0.48)
    assert m.up.spread == pytest.approx(0.02)


def test_outcomes_are_matched_by_name():
    m = PredictMarket.from_payload(payload())
    assert m.up.token_id == "t1"
    assert m.down.token_id == "t2"
    assert m.outcome("nothing") is None


def test_a_market_whose_sides_are_not_up_down_has_no_price():
    """A DEFAULT market's outcomes are named per-question, not Up/Down."""
    m = PredictMarket.from_payload(payload(outcomes=[
        {"name": "Definitely", "onChainId": "a", **book(0.33, 0.37)},
        {"name": "Maybe", "onChainId": "b", **book(0.63, 0.67)}]))
    assert m.up is None
    assert m.up_price is None
    with pytest.raises(PredictFunError, match="Definitely"):
        m.to_quote()


def test_timing_is_read_from_variant_data():
    """A DEFAULT market carries no window; a crypto one has it in variantData."""
    flat = PredictMarket.from_payload(payload(variantData=None))
    assert flat.starts_at is None
    assert not flat.is_five_minute()
    with pytest.raises(PredictFunError, match="variantData"):
        flat.to_quote()


def test_timing_on_the_market_itself_also_works():
    m = PredictMarket.from_payload(payload(
        variantData=None, startsAt=WINDOW, endsAt=WINDOW + 300))
    assert m.window_seconds == 300


def test_an_empty_book_leaves_no_price_to_buy_at():
    m = PredictMarket.from_payload(payload(outcomes=[
        {"name": "Up", "onChainId": "t1"}, {"name": "Down", "onChainId": "t2"}]))
    assert m.up.ask is None
    with pytest.raises(PredictFunError, match="no Up/Down asks"):
        m.to_quote()


def test_an_outcome_with_only_one_side_of_the_book():
    out = Outcome.from_payload({"name": "Up", "onChainId": "t",
                                "bestAsk": {"price": 0.6, "size": 10}})
    assert out.bid is None
    assert out.ask == pytest.approx(0.6)
    assert out.mid == pytest.approx(0.6)
    assert out.spread is None


def test_a_non_five_minute_window_is_not_mistaken_for_one():
    hourly = PredictMarket.from_payload(payload(
        variantData={"startsAt": WINDOW, "endsAt": WINDOW + 3600}))
    assert hourly.window_seconds == 3600
    assert not hourly.is_five_minute()


def test_an_unparseable_market_says_so_rather_than_guessing():
    m = PredictMarket.from_payload({"totally": "unexpected"})
    assert m.starts_at is None
    assert m.outcomes == []
    with pytest.raises(PredictFunError, match="no window start"):
        m.to_quote()


def test_to_quote_hands_the_engine_something_it_can_price():
    m = PredictMarket.from_payload(payload())
    q = m.to_quote(observed_ts=WINDOW + 6)
    assert q.window_start_ts == WINDOW
    assert q.seconds_into_window == 6
    assert q.seconds_to_expiry == 294
    assert q.up_price == pytest.approx(0.49)
    # Both asks together exceed 1.00; that excess is the book's spread.
    assert q.overround == pytest.approx(0.02)


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


def test_the_crypto_variant_enum_is_still_a_guess():
    """Only DEFAULT is confirmed. VariantData_CryptoUpDown names the shape of
    the variantData object, not the marketVariant enum value."""
    assert CRYPTO_UP_DOWN in CRYPTO_VARIANT_CANDIDATES
    assert "VariantData" not in CRYPTO_UP_DOWN


def test_find_variant_returns_the_first_the_api_accepts():
    client = PredictFunClient(testnet=True)
    accepted = CRYPTO_VARIANT_CANDIDATES[1]

    def fake(**params):
        if params.get("marketVariant") != accepted:
            raise PredictFunError("predict.fun returned 400 for /v1/markets")
        return {"success": True, "data": [payload()]}

    client._get_markets = fake
    assert client.find_variant() == accepted


def test_find_variant_gives_up_cleanly_when_none_work():
    client = PredictFunClient(testnet=True)
    client._get_markets = lambda **kw: (_ for _ in ()).throw(
        PredictFunError("predict.fun returned 400 for /v1/markets"))
    assert client.find_variant() is None


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
        PredictMarket.from_payload(payload(id="eth5m",
                                           question="Ethereum Up or Down")),
        PredictMarket.from_payload(payload(id="btc1h", variantData={
            "startsAt": WINDOW, "endsAt": WINDOW + 3600})),
        PredictMarket.from_payload(payload(id="btcClosed", tradingStatus="CLOSED")),
    ])
    assert [m.market_id for m in client.btc_five_minute_markets()] == ["btc5m"]


def test_probe_flags_a_market_with_no_window_times():
    from btc5m.predictfun import probe

    client = PredictFunClient(testnet=True)
    client.find_variant = lambda: None
    client._get_markets = lambda **kw: {"success": True,
                                        "data": [payload(variantData=None)]}
    text = probe(client)
    assert "NO WINDOW TIMES" in text
    assert "_FIELDS" in text
    assert "none of" in text          # variant discovery failed, and says so


def test_probe_shows_a_parsed_market_when_the_shape_matches():
    from btc5m.predictfun import probe

    client = PredictFunClient(testnet=True)
    client.find_variant = lambda: "CRYPTO_UP_DOWN"
    client._get_markets = lambda **kw: {"success": True, "data": [payload()]}
    text = probe(client)
    assert "NO WINDOW TIMES" not in text
    assert "300s" in text
    assert "200.0 bps" in text
    assert "ask 0.49" in text


def test_market_flags_the_order_builder_needs_are_captured():
    """isNegRisk and isYieldBearing come from GET /markets and gate approvals."""
    m = PredictMarket.from_payload(payload(isNegRisk=True, isYieldBearing=True))
    assert m.is_neg_risk is True
    assert m.is_yield_bearing is True
    absent = PredictMarket.from_payload({"id": 1})
    assert absent.is_neg_risk is None      # unknown, not assumed false


def test_the_markets_path_falls_back_when_the_versioned_one_is_absent():
    """The docs write it both as /v1/markets and /markets."""
    from btc5m.predictfun import MARKET_PATHS

    assert MARKET_PATHS == ("/v1/markets", "/markets")
    client = PredictFunClient(testnet=True)
    tried = []

    def fake_get(path, **params):
        tried.append(path)
        if path == "/v1/markets":
            raise PredictFunError("predict.fun returned 404 for /v1/markets")
        return {"data": [payload()]}

    client._get = fake_get
    assert len(client.markets()) == 1
    assert tried == ["/v1/markets", "/markets"]


def test_a_non_404_error_is_not_retried_against_the_other_path():
    client = PredictFunClient(testnet=True)
    tried = []

    def fake_get(path, **params):
        tried.append(path)
        raise PredictFunError("rate limited by predict.fun")

    client._get = fake_get
    with pytest.raises(PredictFunError, match="rate limited"):
        client.markets()
    assert tried == ["/v1/markets"]


def test_status_is_not_sent_by_default():
    """The server rejects status=ACTIVE with a 400 naming MarketStatusFilter."""
    client = PredictFunClient(testnet=True)
    seen = {}

    def fake_get(path, **params):
        seen.update(params)
        return {"success": True, "data": [payload()]}

    client._get = fake_get
    client.markets()
    assert seen.get("status") is None
    assert seen["marketVariant"] == CRYPTO_UP_DOWN


def test_the_success_envelope_is_unwrapped():
    envelope = {"success": True, "data": [payload()]}
    assert len(PredictFunClient._items(envelope)) == 1
