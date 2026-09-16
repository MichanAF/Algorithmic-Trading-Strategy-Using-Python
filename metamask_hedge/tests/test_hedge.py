"""The overlay policy, one guard at a time."""

import pytest

from mmhedge.config import config_from_dict
from mmhedge.hedge import MarketState, carry_component, decide, risk_component


def check(decision, label):
    found = [c for c in decision.checks if c.label == label]
    assert found, f"no check {label!r}; has {[c.label for c in decision.checks]}"
    return found[0]


def state(**kw):
    kw.setdefault("core_usd", 10_000.0)
    return MarketState(**kw)


# -- the two signals -------------------------------------------------------- #

def test_carry_is_silent_below_the_exit_threshold(cfg):
    value, _ = carry_component(state(funding_apr=0.02), cfg)
    assert value == 0.0


def test_carry_ramps_between_enter_and_full(cfg):
    lo, _ = carry_component(state(funding_apr=cfg.carry.enter_apr), cfg)
    mid, _ = carry_component(state(funding_apr=0.30), cfg)
    hi, _ = carry_component(state(funding_apr=cfg.carry.full_apr * 2), cfg)
    assert lo == pytest.approx(0.0)
    assert 0.0 < mid < 1.0
    assert hi == 1.0


def test_baseline_funding_alone_does_not_justify_a_hedge(cfg):
    # ~11% APR is the interest component, not a premium worth hedging for.
    value, _ = carry_component(state(funding_apr=0.11), cfg)
    assert value == 0.0


def test_risk_responds_to_trend_drawdown_and_vol(cfg):
    calm, _ = risk_component(state(trend_score=0.8, vol_rank=0.3), cfg)
    broken, _ = risk_component(state(trend_score=-1.0, vol_rank=0.3), cfg)
    drawn, _ = risk_component(state(trend_score=0.0, core_drawdown=0.35), cfg)
    assert calm == 0.0
    assert broken > 0.5
    assert drawn > 0.3


def test_volatility_shock_is_a_floor_not_a_term(cfg):
    value, _ = risk_component(state(trend_score=1.0, vol_rank=0.99), cfg)
    assert value >= 0.5


# -- combination ------------------------------------------------------------ #

def test_max_combine_lets_either_reason_act_alone(cfg):
    carry_only = decide(state(funding_apr=0.45, trend_score=0.9), cfg)
    risk_only = decide(state(funding_apr=0.0, trend_score=-1.0), cfg)
    assert carry_only.target_ratio > 0.5
    assert risk_only.target_ratio > 0.5


def test_rich_funding_cannot_cancel_a_broken_trend(cfg):
    # The failure mode "max" exists to prevent: netting two reasons to hedge
    # into no hedge at all.
    both = decide(state(funding_apr=0.45, trend_score=-1.0), cfg)
    assert both.target_ratio >= 0.5


def test_target_respects_the_configured_band():
    cfg = config_from_dict({"hedge": {"min_hedge_ratio": 0.2, "max_hedge_ratio": 0.6}})
    hot = decide(state(funding_apr=2.0, trend_score=-1.0), cfg)
    cold = decide(state(funding_apr=0.0, trend_score=1.0), cfg)
    assert hot.target_ratio <= 0.6 + 1e-9
    assert cold.target_ratio >= 0.2 - 1e-9


# -- the guards ------------------------------------------------------------- #

def test_ladder_caps_a_single_move(cfg):
    d = decide(state(funding_apr=1.0, current_ratio=0.0), cfg)
    assert d.target_ratio > 0.9
    assert d.applied_ratio == pytest.approx(1.0 / cfg.hedge.ladder_steps)
    assert check(d, "ladder").passed


def test_minimum_step_suppresses_noise_trades(cfg):
    d = decide(state(funding_apr=0.16, current_ratio=0.03), cfg)
    assert d.action == "hold"
    assert d.delta_notional_usd == 0.0


def test_minimum_hold_blocks_an_early_unwind(cfg):
    d = decide(state(funding_apr=0.16, current_ratio=0.6, days_held=0.2), cfg)
    assert not check(d, "minimum_hold").passed
    assert d.applied_ratio == pytest.approx(0.6)


def test_minimum_hold_releases_once_funding_stops_paying(cfg):
    # The bug this guards: a hedge whose funding decayed to nothing but never
    # went negative could never reach break-even, so the guard would pin it
    # open forever.
    d = decide(state(funding_apr=0.01, current_ratio=0.6, days_held=0.2), cfg)
    assert check(d, "minimum_hold").passed
    assert d.applied_ratio < 0.6


def test_minimum_hold_releases_after_max_hold_days(cfg):
    d = decide(state(funding_apr=0.16, current_ratio=0.6,
                     days_held=cfg.carry.max_hold_days + 1), cfg)
    assert check(d, "minimum_hold").passed


def test_funding_flip_releases_carry_but_keeps_risk(cfg):
    d = decide(state(funding_apr=-0.10, negative_funding_hours=24,
                     trend_score=-1.0, current_ratio=0.6, days_held=5.0), cfg)
    assert not check(d, "funding_flip").passed
    # The trend is still broken, so the risk portion survives the flip.
    assert d.target_ratio > 0.0


def test_funding_flip_with_no_risk_unwinds(cfg):
    d = decide(state(funding_apr=-0.10, negative_funding_hours=24,
                     trend_score=0.8, current_ratio=0.6, days_held=5.0), cfg)
    assert d.target_ratio == pytest.approx(0.0)
    assert d.action == "reduce"


def test_cooldown_blocks_rebuilding_but_allows_cutting(cfg):
    up = decide(state(funding_apr=0.45, current_ratio=0.2, cooldown_active=True), cfg)
    assert not check(up, "deleverage_cooldown").passed
    assert up.action == "hold"

    down = decide(state(funding_apr=0.0, trend_score=1.0, current_ratio=0.6,
                        days_held=99.0, cooldown_active=True), cfg)
    assert check(down, "deleverage_cooldown").passed
    assert down.action == "reduce"


# -- reporting -------------------------------------------------------------- #

def test_maker_pricing_is_cheaper_than_taker(cfg):
    d = decide(state(funding_apr=0.45, core_usd=100_000.0), cfg)
    assert d.est_cost_usd < 0.0          # a rebate
    assert any("taker would cost" in r for r in d.reasons)


def test_report_names_every_check(cfg):
    text = decide(state(funding_apr=0.45), cfg).report()
    for label in ("carry_signal", "risk_signal", "target_ratio", "ladder"):
        assert label in text


def test_infinite_holds_render_as_never(cfg):
    text = decide(state(funding_apr=0.16, current_ratio=0.6, days_held=0.1), cfg).report()
    assert "inf" not in text
