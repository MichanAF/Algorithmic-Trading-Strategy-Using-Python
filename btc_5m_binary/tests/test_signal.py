"""The three betting conditions: agreement, priced edge, timing."""

import numpy as np
import pytest

from btc5m.config import config_from_dict
from btc5m.data import synthetic
from btc5m.features import build_features
from btc5m.gates import DOWN, FLAT, UP
from btc5m.signal import SignalEngine


@pytest.fixture(scope="module")
def rig():
    cfg = config_from_dict()
    series = synthetic(6000, seed=55)
    return cfg, series, build_features(series, cfg), SignalEngine(cfg)


def signals(cfg, series, fs, only_tradable=False):
    engine = SignalEngine(cfg)
    out = []
    for i in range(engine.warmup_bars(fs), len(series)):
        sig = engine.evaluate(fs, i)
        if not only_tradable or sig.tradable:
            out.append(sig)
    return out


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #

def test_engine_splits_the_stack_by_kind(rig):
    cfg, _, _, engine = rig
    assert [g.name for g in engine.veto_gates] == ["data_integrity"]
    assert [g.name for g in engine.directional_gates] == [
        "trend_alignment", "persistence"]
    assert engine.confirm_gates == []


def test_required_features_are_deduplicated(rig):
    _, _, _, engine = rig
    required = engine.required_features
    assert len(required) == len(set(required))
    # One feature from each gate in the default stack.
    assert {"gap_bars", "ema_fast", "variance_ratio"} <= set(required)


def test_break_even_and_odds_agree_with_the_payout(rig):
    cfg, _, _, engine = rig
    assert engine.odds == pytest.approx(0.90)
    assert engine.break_even == pytest.approx(1 / 1.9, abs=1e-6)


def test_every_bar_yields_a_yes_or_a_no(rig):
    cfg, series, fs, _ = rig
    for sig in signals(cfg, series, fs)[:200]:
        assert sig.answer in ("YES", "NO")
        assert (sig.answer == "YES") == sig.tradable


# --------------------------------------------------------------------------- #
# condition 1: agreement
# --------------------------------------------------------------------------- #

def test_a_tradable_signal_has_every_gate_passing(rig):
    cfg, series, fs, _ = rig
    tradable = signals(cfg, series, fs, only_tradable=True)
    assert tradable, "expected at least one tradable signal"
    for sig in tradable:
        assert all(g.passed for g in sig.gate_results)
        directions = {g.direction for g in sig.gate_results if g.is_directional}
        assert directions == {sig.side}
        assert sig.side in (UP, DOWN)


def test_a_failed_veto_blocks_the_bar_and_is_the_only_reason(series):
    """Uses a stack with a veto that actually fires: the default stack's only
    veto is data_integrity, which never fails on a clean feed."""
    cfg = config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                           "trend_alignment", "persistence"]})
    fs = build_features(series, cfg)
    engine = SignalEngine(cfg)
    for i in range(engine.warmup_bars(fs), len(series)):
        sig = engine.evaluate(fs, i)
        vetoes = [g for g in sig.gate_results if g.is_veto and not g.passed]
        if vetoes:
            assert not sig.tradable
            assert sig.blocked_by == ("gate_agreement",)
            assert vetoes[0].name in sig.conditions[0].detail
            return
    pytest.fail("no bar failed a veto gate")


def test_conviction_floor_is_enforced(rig):
    _, series, fs, _ = rig
    counts = []
    for floor in (0.0, 0.5, 0.6, 0.8, 0.95):
        cfg = config_from_dict({"betting": {"min_conviction": floor}})
        counts.append(len(signals(cfg, series, fs, only_tradable=True)))
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > counts[-1]

    # Conviction is a weighted mean of scores in [0, 1], so a floor above 1
    # can never be met and must silence the strategy completely.
    impossible = config_from_dict({"betting": {"min_conviction": 1.01}})
    assert not any(x.tradable for x in signals(impossible, series, fs))


def test_unanimous_mode_rejects_disagreement():
    series = synthetic(8000, seed=77)
    cfg = config_from_dict({"betting": {"required_edge": -1.0, "min_conviction": 0.0}})
    fs = build_features(series, cfg)
    engine = SignalEngine(cfg)
    seen_disagreement = False
    for i in range(engine.warmup_bars(fs), len(series)):
        sig = engine.evaluate(fs, i)
        # Disagreement is only the *reported* reason once nothing earlier in the
        # chain already settled the bar: vetoes run first, and a directional
        # gate that failed blocks for its own reason.
        if not all(g.passed for g in sig.gate_results if g.is_veto):
            continue
        directional = [g for g in sig.gate_results if g.is_directional]
        if not all(g.passed for g in directional):
            continue
        if len({g.direction for g in directional}) > 1:
            seen_disagreement = True
            assert not sig.tradable
            assert "disagree" in sig.conditions[0].detail
            assert sig.side == FLAT
    assert seen_disagreement, "no bar had gates pointing opposite ways"


def test_weighted_mode_can_trade_where_unanimous_will_not():
    series = synthetic(12000, seed=91)
    shared = {"betting": {"required_edge": -1.0, "min_conviction": 0.0,
                          "min_directional_gates": 1}}
    unanimous = config_from_dict(shared)
    weighted = config_from_dict({**shared,
                                "betting": {**shared["betting"], "mode": "weighted",
                                            "allow_dissent": True}})
    fs = build_features(series, unanimous)
    n_unanimous = len(signals(unanimous, series, fs, only_tradable=True))
    n_weighted = len(signals(weighted, series, fs, only_tradable=True))
    assert n_weighted > n_unanimous


def test_min_directional_gates_is_enforced():
    series = synthetic(6000, seed=55)
    cfg = config_from_dict({
        "gate_stack": ["data_integrity", "volatility_regime", "trend_alignment",
                       "persistence", "participation"],
        "betting": {"mode": "weighted", "min_directional_gates": 3,
                    "required_edge": -1.0, "min_conviction": 0.0}})
    fs = build_features(series, cfg)
    for sig in signals(cfg, series, fs, only_tradable=True):
        agreeing = [g for g in sig.gate_results
                    if g.is_directional and g.passed and g.direction == sig.side]
        assert len(agreeing) >= 3


def test_confirmation_gate_can_only_refuse_never_flip():
    series = synthetic(8000, seed=64)
    cfg = config_from_dict({
        "gate_stack": ["data_integrity", "volatility_regime", "trend_alignment",
                       "persistence", "participation", "location"]})
    fs = build_features(series, cfg)
    engine = SignalEngine(cfg)
    for i in range(engine.warmup_bars(fs), len(series)):
        sig = engine.evaluate(fs, i)
        confirms = [g for g in sig.gate_results if g.is_confirm]
        if not confirms:
            continue
        for g in confirms:
            if g.passed:
                directional = {x.direction for x in sig.gate_results
                               if x.is_directional and x.passed}
                assert g.direction in directional


# --------------------------------------------------------------------------- #
# condition 2: priced edge
# --------------------------------------------------------------------------- #

def test_probability_map_is_monotone_and_capped(rig):
    _, _, _, engine = rig
    values = [engine._probability(c) for c in np.linspace(0, 1, 21)]
    assert values == sorted(values)
    assert values[0] == pytest.approx(0.5)
    assert values[-1] == pytest.approx(engine.cfg.betting.prob_cap)


def test_a_worse_payout_raises_the_bar_and_kills_signals():
    series = synthetic(6000, seed=55)
    generous = config_from_dict({"betting": {"net_payout": 0.95}})
    stingy = config_from_dict({"betting": {"net_payout": 0.70}})
    fs = build_features(series, generous)
    assert generous.break_even_probability() < stingy.break_even_probability()
    assert (len(signals(generous, series, fs, only_tradable=True))
            > len(signals(stingy, series, fs, only_tradable=True)))


def test_tradable_signals_always_clear_the_required_edge(rig):
    cfg, series, fs, _ = rig
    for sig in signals(cfg, series, fs, only_tradable=True):
        assert sig.edge >= cfg.betting.required_edge
        assert sig.p_model > sig.break_even
        assert sig.expected_value > 0.0


def test_edge_is_skipped_not_failed_when_no_side_was_proposed(rig):
    cfg, series, fs, engine = rig
    for i in range(engine.warmup_bars(fs), len(series)):
        sig = engine.evaluate(fs, i)
        if sig.side == FLAT:
            edge_condition = next(c for c in sig.conditions if c.label == "priced_edge")
            assert not edge_condition.applicable
            assert "priced_edge" not in sig.blocked_by
            return
    pytest.fail("no bar declined to pick a side")


def test_contract_price_mode_prices_break_even_as_the_contract_price():
    cfg = config_from_dict({"betting": {"payout_mode": "contract_price",
                                        "contract_price": 0.55, "fee_bps": 0.0}})
    assert cfg.break_even_probability() == pytest.approx(0.55)
    assert cfg.payoff_odds() == pytest.approx(0.45 / 0.55)


def test_fees_make_the_bar_higher():
    free = config_from_dict({"betting": {"payout_mode": "contract_price",
                                         "contract_price": 0.52, "fee_bps": 0.0}})
    costly = config_from_dict({"betting": {"payout_mode": "contract_price",
                                          "contract_price": 0.52, "fee_bps": 50.0}})
    assert costly.break_even_probability() > free.break_even_probability()
    assert costly.payoff_odds() < free.payoff_odds()


# --------------------------------------------------------------------------- #
# condition 3: timing
# --------------------------------------------------------------------------- #

def test_a_stale_signal_is_refused(rig):
    cfg, series, fs, engine = rig
    tradable = signals(cfg, series, fs, only_tradable=True)[0]
    late = engine.evaluate(fs, tradable.bar_index,
                           now_ts=tradable.ts + cfg.betting.max_signal_age_seconds + 1)
    assert not late.tradable
    assert "timing" in late.blocked_by


def test_too_little_time_to_expiry_is_refused(rig):
    cfg, series, fs, engine = rig
    tradable = signals(cfg, series, fs, only_tradable=True)[0]
    b = cfg.betting
    almost_expired = tradable.ts + b.horizon_bars * b.bar_seconds - 1
    result = engine.evaluate(fs, tradable.bar_index, now_ts=almost_expired)
    assert not result.tradable
    assert "timing" in result.blocked_by


def test_a_longer_horizon_leaves_more_time(rig):
    """Late in a 5-minute bet there is no time left; in a 15-minute one there is."""
    _, series, fs, _ = rig
    late = 260                                   # seconds after the bar closed
    shared = {"max_signal_age_seconds": 300}

    def timing_at(horizon_bars):
        cfg = config_from_dict({"betting": {**shared, "horizon_bars": horizon_bars}})
        engine = SignalEngine(cfg)
        i = engine.warmup_bars(fs) + 10
        sig = engine.evaluate(fs, i, now_ts=int(series.ts[i]) + late)
        return next(c for c in sig.conditions if c.label == "timing")

    assert not timing_at(1).passed                # 40s left, under the 60s floor
    assert timing_at(3).passed                    # 640s left


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def test_report_lists_every_gate_and_every_condition(rig):
    cfg, series, fs, _ = rig
    sig = signals(cfg, series, fs, only_tradable=True)[0]
    text = sig.report()
    for gate in sig.gate_results:
        assert gate.name in text
        for c in gate.checks:
            assert c.label in text
    for c in sig.conditions:
        assert c.label in text
    assert "YES" in text
