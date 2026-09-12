"""Contract-priced venues: live quotes, the window clock, and gas."""

import pytest

from btc5m.config import config_from_dict, load_config
from btc5m.data import synthetic
from btc5m.features import build_features
from btc5m.gates import DOWN, FLAT, UP
from btc5m.risk import RiskManager
from btc5m.signal import SignalEngine
from btc5m.venue import (TRUST_WALLET_BTC_5M, MarketQuote, VenueRules,
                         evaluate_market, minimum_viable_stake)

WINDOW = 1_757_675_700          # a 5-minute boundary


@pytest.fixture(scope="module")
def rig():
    cfg = config_from_dict({"betting": {"payout_mode": "contract_price",
                                        "contract_price": 0.50}})
    series = synthetic(20_000, seed=11)
    fs = build_features(series, cfg)
    engine = SignalEngine(cfg)
    index = next(i for i in range(engine.warmup_bars(fs), len(series))
                 if engine.evaluate(fs, i).tradable)
    return cfg, series, fs, engine, index


def quote_at(seconds_in, down=0.51, overround=0.0, window=WINDOW):
    return MarketQuote.from_percent(window, down, observed_ts=window + seconds_in,
                                    overround=overround)


def condition(decision, label):
    found = [c for c in decision.conditions if c.label == label]
    assert found, f"no condition {label!r}; has {[c.label for c in decision.conditions]}"
    return found[0]


# --------------------------------------------------------------------------- #
# quotes
# --------------------------------------------------------------------------- #

def test_a_percentage_becomes_two_complementary_prices():
    q = MarketQuote.from_percent(WINDOW, 84, observed_ts=WINDOW + 10)
    assert q.down_price == pytest.approx(0.84)
    assert q.up_price == pytest.approx(0.16)
    assert q.overround == pytest.approx(0.0)


def test_a_probability_and_a_percent_are_both_accepted():
    a = MarketQuote.from_percent(WINDOW, 84, observed_ts=WINDOW)
    b = MarketQuote.from_percent(WINDOW, 0.84, observed_ts=WINDOW)
    assert a.down_price == pytest.approx(b.down_price)


def test_overround_is_charged_to_both_sides():
    q = MarketQuote.from_percent(WINDOW, 50, observed_ts=WINDOW, overround=0.04)
    assert q.overround == pytest.approx(0.04)
    assert q.up_price == pytest.approx(0.52)
    assert q.down_price == pytest.approx(0.52)


def test_prices_outside_zero_to_one_are_rejected():
    with pytest.raises(ValueError, match="up_price"):
        MarketQuote(WINDOW, up_price=1.2, down_price=0.5, observed_ts=WINDOW)
    with pytest.raises(ValueError, match="down_price"):
        MarketQuote(WINDOW, up_price=0.5, down_price=0.0, observed_ts=WINDOW)


def test_the_window_clock():
    q = quote_at(120)
    assert q.window_end_ts == WINDOW + 300
    assert q.seconds_into_window == 120
    assert q.seconds_to_expiry == 180


def test_skew_measures_distance_from_an_even_market():
    assert quote_at(5, down=50).skew == pytest.approx(0.0)
    assert quote_at(5, down=84).skew == pytest.approx(0.34)


def test_price_for_rejects_a_missing_side():
    q = quote_at(5)
    assert q.price_for(UP) == pytest.approx(q.up_price)
    assert q.price_for(DOWN) == pytest.approx(q.down_price)
    with pytest.raises(ValueError):
        q.price_for(FLAT)


# --------------------------------------------------------------------------- #
# the screenshot market
# --------------------------------------------------------------------------- #

def test_a_decided_market_late_in_the_window_is_refused(rig):
    """The exact case in the screenshots: 84/16 with 47 seconds left."""
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    q = quote_at(253, down=84, window=int(fs.series.ts[index]))
    decision = evaluate_market(signal, q, cfg)
    assert not decision.approved
    assert not condition(decision, "entry_window").passed
    assert not condition(decision, "time_to_expiry").passed
    assert not condition(decision, "market_not_decided").passed
    assert "already priced the move" in condition(decision, "market_not_decided").detail


def test_an_even_market_at_the_open_is_taken(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    q = quote_at(8, down=51, window=int(fs.series.ts[index]))
    decision = evaluate_market(signal, q, cfg)
    assert decision.approved
    assert decision.side == signal.side
    assert decision.edge > cfg.betting.required_edge


def test_the_skew_veto_fires_even_early_in_the_window(rig):
    """Being early is not enough: a lopsided quote still means it knows more."""
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    q = quote_at(5, down=80, window=int(fs.series.ts[index]))
    decision = evaluate_market(signal, q, cfg)
    assert not condition(decision, "market_not_decided").passed


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #

def test_break_even_is_the_price_paid(rig):
    """The whole point of a contract market: you need to beat what you pay."""
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    dear = evaluate_market(signal, quote_at(5, down=57, window=window), cfg)
    cheap = evaluate_market(signal, quote_at(5, down=45, window=window), cfg)
    assert cheap.edge > dear.edge
    assert cheap.edge == pytest.approx(signal.p_model - cheap.price)


def test_a_price_above_the_probability_cap_can_never_be_bought(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    # prob_cap is 0.64, so nothing near 0.64 clears required_edge on top.
    decision = evaluate_market(signal, quote_at(5, down=62, window=window), cfg)
    assert not condition(decision, "priced_edge").passed


def test_fees_are_added_to_the_price(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    q = quote_at(5, down=50, window=window)
    free = evaluate_market(signal, q, cfg, rules=TRUST_WALLET_BTC_5M)
    costly_rules = VenueRules(name="x", fee_bps=100.0, max_entry_skew=0.5)
    costly = evaluate_market(signal, q, cfg, rules=costly_rules)
    assert costly.price > free.price
    assert costly.edge < free.edge


def test_the_other_side_is_reported_but_not_taken(rig):
    """A lopsided quote makes the opposite side look cheap. That is the trap."""
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    # Price the side we want expensively, so the opposite one looks like a steal.
    q = MarketQuote.from_percent(window, 85 if signal.side == DOWN else 15,
                                 observed_ts=window + 5)
    decision = evaluate_market(signal, q, cfg)
    assert decision.other_side_edge is not None
    assert decision.other_side_edge > 0          # looks attractive
    assert decision.side == signal.side          # still not taken
    assert "betting against our own signal" in decision.report()


# --------------------------------------------------------------------------- #
# sizing and gas
# --------------------------------------------------------------------------- #

def test_the_stake_is_sized_with_the_live_contract_odds(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    q = quote_at(5, down=40, window=window)
    decision = evaluate_market(signal, q, cfg, risk=risk)
    expected_odds = (1.0 - decision.price) / decision.price
    assert f"{expected_odds:.3f}x" in decision.risk.conditions[0].detail


def test_contracts_follow_from_stake_and_price(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    decision = evaluate_market(signal, quote_at(5, down=48, window=window),
                               cfg, risk=risk)
    assert decision.approved
    assert decision.contracts == pytest.approx(decision.stake / decision.price)


def test_gas_larger_than_the_edge_refuses_the_bet(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    tiny = config_from_dict({"betting": {"payout_mode": "contract_price",
                                         "contract_price": 0.50},
                             "risk": {"starting_bankroll": 100.0,
                                      "max_stake_pct": 0.02, "min_stake": 0.5}})
    greedy = VenueRules(name="expensive", gas_cost_quote=500.0, max_entry_skew=0.5)
    risk = RiskManager(tiny.risk, engine.break_even, engine.odds)
    decision = evaluate_market(signal, quote_at(5, down=48, window=window),
                               tiny, risk=risk, rules=greedy)
    assert not decision.approved
    assert decision.blocked_by == ("gas_cost",)


def test_minimum_viable_stake_scales_with_gas_and_shrinks_with_edge():
    rules = VenueRules(name="x", gas_cost_quote=0.30)
    small_edge = minimum_viable_stake(0.02, 0.50, rules)
    big_edge = minimum_viable_stake(0.20, 0.50, rules)
    assert small_edge > big_edge
    expensive = minimum_viable_stake(0.02, 0.50,
                                     VenueRules(name="y", gas_cost_quote=3.0))
    assert expensive == pytest.approx(small_edge * 10)
    assert minimum_viable_stake(-0.01, 0.5, rules) == float("inf")


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #

def test_the_bundled_venue_config_loads_and_prices_a_contract():
    cfg = load_config("configs/trustwallet-bnb-5m.json")
    cfg.validate()
    assert cfg.betting.payout_mode == "contract_price"
    assert cfg.betting.tie_policy == "void"
    assert cfg.break_even_probability() == pytest.approx(cfg.betting.contract_price)


def test_the_trust_wallet_profile_matches_the_published_rules():
    v = TRUST_WALLET_BTC_5M
    assert v.window_seconds == 300
    assert v.settlement_candle_seconds == 300
    assert v.quote_style == "contract_price"
    assert v.tie_rule == "split"
    assert "chain.link" in v.settlement_url
    assert "BNB" in v.chain


def test_a_flat_signal_produces_no_bet(rig):
    cfg, _, fs, engine, _ = rig
    flat = next(engine.evaluate(fs, i) for i in range(engine.warmup_bars(fs), 3000)
                if engine.evaluate(fs, i).side == FLAT)
    decision = evaluate_market(flat, quote_at(5), cfg)
    assert not decision.approved
    assert not condition(decision, "gate_signal").passed
    assert decision.answer == "NO"


# --------------------------------------------------------------------------- #
# the one-line answer
# --------------------------------------------------------------------------- #

def test_brief_names_the_side_the_price_and_the_stake(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    risk = RiskManager(cfg.risk, engine.break_even, engine.odds)
    decision = evaluate_market(signal, quote_at(6, down=49, window=window),
                               cfg, risk=risk)
    line = decision.brief()
    assert line.startswith(("UP", "DOWN"))
    assert "buy at" in line and "stake" in line and "left" in line
    assert "\n" not in line


def test_brief_prefers_the_informative_reason_over_the_first_one(rig):
    """Late and decided are the same event; "the market knows" is the useful half."""
    cfg, _, fs, engine, index = rig
    window = int(fs.series.ts[index])
    quote = quote_at(253, down=84, window=window)

    # Several conditions fail at once, and the clock ones come first in order.
    fresh_signal = engine.evaluate(fs, index)
    decision = evaluate_market(fresh_signal, quote, cfg)
    assert {"entry_window", "time_to_expiry", "market_not_decided"} <= set(
        decision.blocked_by)
    assert "market already decided" in decision.brief()

    # And the same when the signal itself is stale, which is what the CLI does:
    # it decides as of the moment the quote was observed.
    stale_signal = engine.evaluate(fs, index, now_ts=quote.observed_ts)
    assert not stale_signal.tradable
    stale = evaluate_market(stale_signal, quote, cfg)
    assert "gate_signal" in stale.blocked_by
    assert "market already decided" in stale.brief()


def test_brief_reports_the_price_when_that_is_what_blocked_it(rig):
    cfg, _, fs, engine, index = rig
    signal = engine.evaluate(fs, index)
    window = int(fs.series.ts[index])
    decision = evaluate_market(signal, quote_at(6, down=58, window=window), cfg)
    line = decision.brief()
    assert "too high" in line
    assert "nan" not in line.lower()


def test_brief_names_the_blocking_gate_when_there_is_no_side(rig):
    cfg, _, fs, engine, _ = rig
    flat_index = next(i for i in range(engine.warmup_bars(fs), 3000)
                      if engine.evaluate(fs, i).side == FLAT)
    signal = engine.evaluate(fs, flat_index)
    window = int(fs.series.ts[flat_index])
    decision = evaluate_market(signal, quote_at(6, window=window), cfg)
    line = decision.brief()
    assert line.startswith("NO BET")
    assert "nan" not in line.lower()
    # Should name a real gate or condition, not a generic refusal.
    assert decision.signal_reason
    assert decision.signal_reason in line


def test_signal_brief_is_one_line_either_way(rig):
    _, _, fs, engine, index = rig
    tradable = engine.evaluate(fs, index).brief()
    assert tradable.startswith(("UP", "DOWN"))
    assert "conviction" in tradable
    blocked = next(engine.evaluate(fs, i).brief()
                   for i in range(engine.warmup_bars(fs), 3000)
                   if not engine.evaluate(fs, i).tradable)
    assert blocked.startswith("NO BET")
    assert "\n" not in blocked


# --------------------------------------------------------------------------- #
# no volatility band on a contract market
# --------------------------------------------------------------------------- #

def test_the_venue_config_has_no_volatility_gate():
    """A yes/no bet crosses no spread, so a move-size floor buys nothing.

    Measured over 1,042 unseen days, accuracy is the same either side of the
    band. The tail guard lives in risk.atr_shock_rank instead.
    """
    cfg = load_config("configs/trustwallet-bnb-5m.json")
    assert "volatility_regime" not in cfg.gate_stack
    assert cfg.risk.atr_shock_rank < 1.0
    assert 3 <= len(cfg.gate_stack) <= 5


def test_the_volatility_shock_veto_still_works_without_the_gate():
    cfg = load_config("configs/trustwallet-bnb-5m.json")
    risk = RiskManager(cfg.risk, cfg.break_even_probability(), cfg.payoff_odds())
    calm = risk.assess(bar_index=1, ts=WINDOW, p_model=0.60, atr_rank=0.5)
    shock = risk.assess(bar_index=1, ts=WINDOW, p_model=0.60, atr_rank=0.999)
    assert calm.approved
    assert not shock.approved
    assert "strategy_health" in shock.blocked_by


def test_configs_may_carry_underscore_notes(tmp_path):
    import json
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"_why": "a note for the reader",
                             "betting": {"_note": "also fine", "net_payout": 0.93}}))
    cfg = load_config(p)
    assert cfg.betting.net_payout == pytest.approx(0.93)
