"""Each gate passes and fails for the reason it claims to."""

import numpy as np
import pytest

from btc5m.config import config_from_dict
from btc5m.data import BAR_SECONDS, BarSeries, synthetic
from btc5m.features import build_features
from btc5m.gates import (DOWN, FLAT, GATE_REGISTRY, UP, Check, build_stack,
                         direction_name)

BASE_TS = 1_700_000_000


def make_series(close, volume=None, spread_bps=None, wick=0.001, start_ts=BASE_TS):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate(([close[0]], close[:-1]))
    pad = close * wick
    return BarSeries(
        ts=start_ts + np.arange(n, dtype=np.int64) * BAR_SECONDS,
        open=open_,
        high=np.maximum(open_, close) + pad,
        low=np.minimum(open_, close) - pad,
        close=close,
        volume=np.full(n, 100.0) if volume is None else np.asarray(volume, float),
        spread_bps=spread_bps,
    )


def uptrend(n=600, rate=0.0006, start=60_000.0):
    return start * np.exp(np.arange(n) * rate)


def persistent_returns(n=800, phi=0.5, sigma=0.0008, drift=0.0002, seed=3):
    """Prices whose returns are positively autocorrelated -- a real trend.

    Built by compounding an AR(1) return process.  Noise must go into the
    returns, not the price level: multiplying iid noise onto prices produces a
    differenced series with strongly negative autocorrelation, which is mean
    reversion, not trend.
    """
    rng = np.random.default_rng(seed)
    shock = sigma * rng.standard_normal(n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + shock[i]
    return 60_000.0 * np.exp(np.cumsum(r + drift))


def alternating(n=800, step=0.0006):
    """Prices that reverse every bar -- the cleanest possible chop."""
    r = np.where(np.arange(n) % 2 == 0, step, -step)
    return 60_000.0 * np.exp(np.cumsum(r))


def evaluate(name, series, overrides=None, index=-1, proposed=FLAT):
    cfg = config_from_dict(overrides or {})
    fs = build_features(series, cfg)
    i = index if index >= 0 else len(series) + index
    gate = GATE_REGISTRY[name]
    return gate.evaluate(fs, i, cfg.gates, proposed)


def check(result, label):
    found = [c for c in result.checks if c.label == label]
    assert found, f"{result.name} has no check {label!r}; has {[c.label for c in result.checks]}"
    return found[0]


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #

def test_every_registered_gate_declares_a_known_kind():
    for name, gate in GATE_REGISTRY.items():
        assert gate.kind in ("veto", "directional", "confirm"), name
        assert gate.name == name
        assert gate.requires, f"{name} declares no inputs"
        assert gate.params_key, f"{name} declares no params key"


def test_build_stack_orders_veto_then_directional_then_confirm():
    stack = build_stack(["location", "participation", "data_integrity",
                         "trend_alignment", "volatility_regime"])
    kinds = [g.kind for g in stack]
    assert kinds == ["veto", "veto", "directional", "directional", "confirm"]


def test_build_stack_rejects_unknown_gate():
    with pytest.raises(ValueError, match="unknown gate"):
        build_stack(["data_integrity", "not_a_gate"])


def test_direction_name_covers_all_three_states():
    assert direction_name(UP) == "UP"
    assert direction_name(DOWN) == "DOWN"
    assert direction_name(FLAT) == "FLAT"


def test_gate_reports_warmup_rather_than_passing_on_nan():
    result = evaluate("trend_alignment", make_series(uptrend(600)), index=5)
    assert not result.passed
    assert result.note == "warming up"
    assert result.direction == FLAT


# --------------------------------------------------------------------------- #
# data integrity
# --------------------------------------------------------------------------- #

def test_data_integrity_passes_on_a_clean_bar():
    assert evaluate("data_integrity", make_series(uptrend())).passed


def test_data_integrity_catches_a_missing_bar():
    close = uptrend(400)
    ts = BASE_TS + np.arange(len(close), dtype=np.int64) * BAR_SECONDS
    ts[-1] += 3 * BAR_SECONDS                     # a gap before the last bar
    series = make_series(close)
    series.ts = ts
    cfg = config_from_dict()
    fs = build_features(series, cfg)
    result = GATE_REGISTRY["data_integrity"].evaluate(fs, len(close) - 1, cfg.gates)
    assert not result.passed
    assert not check(result, "no_bar_gap").passed


def test_data_integrity_catches_a_stuck_feed():
    close = np.concatenate([uptrend(400), [60_000.0] * 4])
    result = evaluate("data_integrity", make_series(close))
    assert not check(result, "feed_not_stale").passed


def test_data_integrity_catches_a_wide_spread():
    close = uptrend(400)
    spread = np.zeros(len(close)); spread[-1] = 25.0
    result = evaluate("data_integrity", make_series(close, spread_bps=spread))
    assert not check(result, "spread_ok").passed


def test_data_integrity_catches_a_zero_volume_bar():
    close = uptrend(400)
    vol = np.full(len(close), 100.0); vol[-1] = 0.0
    result = evaluate("data_integrity", make_series(close, volume=vol))
    assert not check(result, "has_volume").passed


# --------------------------------------------------------------------------- #
# volatility regime
# --------------------------------------------------------------------------- #

def test_volatility_regime_rejects_a_dead_market():
    result = evaluate("volatility_regime", make_series(uptrend(600, rate=1e-7),
                                                      wick=1e-7))
    assert not result.passed
    assert not check(result, "move_covers_cost").passed


def test_volatility_regime_rejects_a_volatility_spike():
    close = uptrend(600, rate=0.0002).copy()
    close[-30:] *= np.exp(np.linspace(0, 0.08, 30))       # sudden expansion
    result = evaluate("volatility_regime", make_series(close, wick=0.004))
    assert not result.passed


def test_volatility_regime_scores_highest_mid_band():
    series = synthetic(3000, seed=5)
    cfg = config_from_dict()
    fs = build_features(series, cfg)
    gate = GATE_REGISTRY["volatility_regime"]
    passing = [gate.evaluate(fs, i, cfg.gates) for i in range(400, len(series))]
    passing = [r for r in passing if r.passed]
    assert passing, "expected some bars inside the volatility band"
    assert all(0.0 <= r.score <= 1.0 for r in passing)


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #

def test_session_gate_blocks_disallowed_hours():
    series = make_series(uptrend(400))
    hour = int((series.ts[-1] // 3600) % 24)
    allowed = [h for h in range(24) if h != hour]
    result = evaluate("session", series,
                      {"gates": {"session": {"allowed_hours_utc": allowed}}})
    assert not check(result, "hour_allowed").passed


def test_session_gate_blocks_the_funding_window():
    # 08:00:00 UTC exactly, a funding boundary.
    start = 1_700_000_000 - (1_700_000_000 % 86_400) + 8 * 3600
    close = uptrend(400)
    series = make_series(close, start_ts=start - (len(close) - 1) * BAR_SECONDS)
    result = evaluate("session", series)
    assert not check(result, "outside_funding_window").passed


def test_session_gate_can_skip_weekends():
    series = make_series(uptrend(400))
    weekday = int(((series.ts[-1] // 86_400) + 4) % 7)
    result = evaluate("session", series,
                      {"gates": {"session": {"skip_weekend": True}}})
    assert check(result, "weekday_only").passed == (weekday < 5)


# --------------------------------------------------------------------------- #
# trend alignment
# --------------------------------------------------------------------------- #

def test_trend_alignment_votes_up_in_an_uptrend():
    result = evaluate("trend_alignment", make_series(uptrend()))
    assert result.passed
    assert result.direction == UP


def test_trend_alignment_votes_down_in_a_downtrend():
    result = evaluate("trend_alignment", make_series(uptrend()[::-1].copy()))
    assert result.passed
    assert result.direction == DOWN


def test_trend_alignment_refuses_a_flat_market():
    result = evaluate("trend_alignment", make_series(np.full(600, 60_000.0)))
    assert not result.passed
    assert result.direction == FLAT


def test_trend_alignment_requires_the_higher_timeframe_to_agree():
    # A 30-minute bounce inside a long downtrend: 5m has turned, 15m has not.
    close = np.concatenate([uptrend(500)[::-1], uptrend(6, rate=0.004)])
    result = evaluate("trend_alignment", make_series(close))
    assert not result.passed

    # Extend the same bounce to three hours and the 15m chart really has
    # turned, so the gate should now allow it.  Guards against the test
    # passing for the wrong reason.
    longer = np.concatenate([uptrend(500)[::-1], uptrend(40, rate=0.004)])
    assert evaluate("trend_alignment", make_series(longer)).passed


def test_trend_alignment_r2_threshold_is_enforced():
    strict = evaluate("trend_alignment", make_series(synthetic(800, seed=9).close),
                      {"gates": {"trend_alignment": {"min_r2": 0.99}}})
    assert not check(strict, "move_is_orderly").passed


# --------------------------------------------------------------------------- #
# momentum thrust
# --------------------------------------------------------------------------- #

def test_momentum_thrust_fires_on_a_sharp_push():
    rng = np.random.default_rng(1)
    quiet = 60_000.0 * np.exp(rng.normal(0, 0.0004, 500).cumsum())
    push = quiet[-1] * np.exp(np.cumsum([0.0010, 0.0010, 0.0010]))
    result = evaluate("momentum_thrust", make_series(np.concatenate([quiet, push])))
    assert result.passed
    assert result.direction == UP
    assert check(result, "thrust_is_unusual").passed
    assert check(result, "rsi_in_band").passed

    # Three times the push exhausts the RSI, and the gate stands down --
    # the ceiling is the half of this gate that keeps it from buying tops.
    spent = quiet[-1] * np.exp(np.cumsum([0.003, 0.003, 0.003]))
    exhausted = evaluate("momentum_thrust",
                         make_series(np.concatenate([quiet, spent])))
    assert not exhausted.passed
    assert not check(exhausted, "rsi_in_band").passed


def test_momentum_thrust_refuses_an_exhausted_push():
    close = uptrend(600, rate=0.004)             # RSI pinned near 100
    result = evaluate("momentum_thrust", make_series(close))
    assert not result.passed
    assert not check(result, "rsi_in_band").passed


def test_momentum_thrust_refuses_an_indecisive_candle():
    rng = np.random.default_rng(4)
    close = 60_000 * np.exp(rng.normal(0, 0.0004, 600).cumsum())
    result = evaluate("momentum_thrust", make_series(close, wick=0.02))
    assert not check(result, "candle_is_decisive").passed


# --------------------------------------------------------------------------- #
# participation
# --------------------------------------------------------------------------- #

def test_participation_refuses_thin_volume():
    close = uptrend(500)
    vol = np.full(len(close), 100.0); vol[-1] = 10.0
    result = evaluate("participation", make_series(close, volume=vol))
    assert not check(result, "volume_confirms").passed


def test_participation_refuses_a_blowoff_bar():
    close = uptrend(500)
    vol = np.full(len(close), 100.0); vol[-1] = 5_000.0
    result = evaluate("participation", make_series(close, volume=vol))
    assert not check(result, "not_a_blowoff").passed


def test_participation_confirms_a_rising_volume_uptrend():
    close = uptrend(500)
    vol = np.full(len(close), 100.0); vol[-1] = 160.0
    result = evaluate("participation", make_series(close, volume=vol))
    assert result.passed
    assert result.direction == UP


# --------------------------------------------------------------------------- #
# location (confirmation)
# --------------------------------------------------------------------------- #

def test_location_needs_a_proposed_side():
    result = evaluate("location", make_series(uptrend(500)), proposed=FLAT)
    assert not result.passed
    assert result.note == "nothing to confirm"


def test_location_confirms_the_side_it_is_given_and_never_flips_it():
    series = make_series(uptrend(500))
    for side in (UP, DOWN):
        result = evaluate("location", series, proposed=side)
        assert result.direction in (side, FLAT)


def test_location_refuses_a_chase_far_from_vwap():
    close = np.concatenate([np.full(480, 60_000.0), uptrend(40, rate=0.01,
                                                            start=60_000.0)])
    result = evaluate("location", make_series(close), proposed=UP)
    assert not check(result, "not_chasing").passed


# --------------------------------------------------------------------------- #
# persistence and mean reversion
# --------------------------------------------------------------------------- #

def test_persistence_passes_in_a_trend_and_fails_in_chop():
    trend = evaluate("persistence",
                     make_series(persistent_returns(drift=0.001, sigma=0.0004)))
    assert trend.passed
    assert trend.direction == UP
    assert check(trend, "returns_trend").passed

    chop = evaluate("persistence", make_series(alternating()))
    assert not chop.passed
    assert not check(chop, "returns_trend").passed


def test_mean_reversion_and_persistence_cannot_both_pass():
    """They require opposite variance ratios, so a stack with both never fires."""
    series = synthetic(4000, seed=17)
    cfg = config_from_dict()
    fs = build_features(series, cfg)
    both = 0
    for i in range(400, len(series)):
        mr = GATE_REGISTRY["mean_reversion"].evaluate(fs, i, cfg.gates)
        pers = GATE_REGISTRY["persistence"].evaluate(fs, i, cfg.gates)
        if mr.passed and pers.passed:
            both += 1
    assert both == 0


def test_mean_reversion_fades_a_stretched_move():
    rng = np.random.default_rng(6)
    close = 60_000 * np.exp(rng.normal(0, 0.0006, 600).cumsum())
    close[-1] = close[-2] * 1.02                    # a spike up
    vol = np.full(600, 100.0); vol[-1] = 400.0
    result = evaluate("mean_reversion", make_series(close, volume=vol))
    assert result.direction == DOWN                 # fade, not follow


# --------------------------------------------------------------------------- #
# cross asset
# --------------------------------------------------------------------------- #

def test_cross_asset_says_so_when_no_reference_is_supplied():
    result = evaluate("cross_asset", make_series(uptrend(500)), proposed=UP)
    assert not result.passed
    assert result.note == "no reference data"


def test_cross_asset_confirms_agreement_and_refuses_disagreement():
    cfg = config_from_dict()
    btc = make_series(uptrend(500))
    up_ref = make_series(uptrend(500, rate=0.001))
    down_ref = make_series(uptrend(500, rate=0.001)[::-1].copy())
    gate = GATE_REGISTRY["cross_asset"]

    agree = gate.evaluate(build_features(btc, cfg, up_ref), 499, cfg.gates, UP)
    assert agree.passed

    disagree = gate.evaluate(build_features(btc, cfg, down_ref), 499, cfg.gates, UP)
    assert not disagree.passed
    assert not check(disagree, "reference_agrees").passed


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def test_check_renders_three_states():
    assert str(Check("a", True, "x")).startswith("PASS")
    assert str(Check("a", False, "x")).startswith("FAIL")
    assert str(Check("a", False, "x", applicable=False)).startswith("SKIP")


def test_gate_scores_are_bounded_and_summaries_are_readable():
    series = synthetic(2000, seed=13)
    cfg = config_from_dict()
    fs = build_features(series, cfg)
    for name, gate in GATE_REGISTRY.items():
        result = gate.evaluate(fs, len(series) - 1, cfg.gates, UP)
        assert 0.0 <= result.score <= 1.0, name
        assert name in result.summary()
        assert result.failed_checks == tuple(c for c in result.checks if not c.passed)
