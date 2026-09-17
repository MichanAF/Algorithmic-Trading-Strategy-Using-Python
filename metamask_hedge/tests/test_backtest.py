"""The engine: does it pay every fee, and does the overlay do what it claims?"""

import numpy as np
import pytest

from mmhedge.backtest import build_signals, compare, render_table, run_backtest
from mmhedge.config import config_from_dict
from mmhedge.data import Series, synthetic


def test_policies_run_and_conserve_the_starting_capital(cfg, series):
    for policy in ("none", "adaptive", "neutral"):
        r = run_backtest(series, cfg, policy=policy)
        assert len(r.equity) == len(series)
        # equity[0] carries one hour of mUSD accrual, which is genuinely earned.
        assert r.equity[0] == pytest.approx(r.capital, rel=1e-4)
        assert np.all(np.isfinite(r.equity))


def test_core_only_never_touches_the_perp(cfg, series):
    r = run_backtest(series, cfg, policy="none")
    assert r.trades == 0
    assert r.funding_collected == 0.0
    assert r.liquidations == 0
    assert np.all(r.hedge_ratio == 0.0)


def test_the_overlay_cuts_volatility_and_drawdown(cfg, series):
    core, overlay, _ = compare(series, cfg)
    # This is the mechanical claim, and the only one that survives decoupling
    # funding from price. Carrying less delta means less variance.
    assert overlay.ann_vol < core.ann_vol
    assert overlay.max_drawdown < core.max_drawdown


def test_the_overlay_actually_hedges(cfg, series):
    r = run_backtest(series, cfg, policy="adaptive")
    assert 0.0 < r.avg_hedge_ratio < 1.0
    assert r.trades > 0


def test_neutral_policy_collects_more_funding_than_the_overlay(cfg, series):
    _, overlay, neutral = compare(series, cfg)
    assert neutral.funding_collected > overlay.funding_collected


def test_the_deleverage_hatch_prevents_liquidations_in_a_rally(cfg):
    # A relentless one-way rally is the worst case for a short hedge. With the
    # last resort on, the position is trimmed rather than liquidated.
    hours = 24 * 200
    price = 50_000.0 * np.exp(np.linspace(0.0, np.log(4.0), hours))
    s = Series(ts=np.arange(hours, dtype=np.int64) * 3600, price=price,
               funding=np.full(hours, 0.00004))
    guarded = run_backtest(s, cfg, policy="neutral")
    assert guarded.deleverages > 0
    assert guarded.liquidations == 0

    bare = config_from_dict({"capital_usd": 25_000.0,
                             "risk": {"margin_last_resort": "none"}})
    assert run_backtest(s, bare, policy="neutral").liquidations > 0


def test_liquidation_leaves_the_core_unhedged(cfg):
    hours = 24 * 120
    price = 50_000.0 * np.exp(np.linspace(0.0, np.log(3.0), hours))
    s = Series(ts=np.arange(hours, dtype=np.int64) * 3600, price=price,
               funding=np.zeros(hours))
    bare = config_from_dict({"capital_usd": 25_000.0,
                             "risk": {"margin_last_resort": "none"}})
    r = run_backtest(s, bare, policy="neutral")
    assert r.liquidations > 0
    assert any("unhedged" in e for e in r.events)


def test_negative_funding_is_paid_not_received(cfg):
    hours = 24 * 60
    s = Series(ts=np.arange(hours, dtype=np.int64) * 3600,
               price=np.full(hours, 50_000.0),
               funding=np.full(hours, -0.00002))
    r = run_backtest(s, cfg, policy="neutral")
    assert r.funding_collected < 0.0


def test_taker_fills_cost_more_than_maker_fills(series):
    maker = config_from_dict({"capital_usd": 25_000.0,
                              "hedge": {"use_maker_orders": True}})
    taker = config_from_dict({"capital_usd": 25_000.0,
                              "hedge": {"use_maker_orders": False}})
    assert (run_backtest(series, taker, policy="adaptive").fees_paid
            > run_backtest(series, maker, policy="adaptive").fees_paid)


def test_the_cooldown_reduces_churn():
    s = synthetic(24 * 365, seed=14)          # a strong, squeezy rally
    hot = config_from_dict({"capital_usd": 25_000.0,
                            "risk": {"deleverage_cooldown_hours": 0}})
    cool = config_from_dict({"capital_usd": 25_000.0,
                             "risk": {"deleverage_cooldown_hours": 72}})
    assert (run_backtest(s, cool, policy="adaptive").trades
            < run_backtest(s, hot, policy="adaptive").trades)


def test_signals_are_bounded(cfg, series):
    sig = build_signals(series, cfg)
    assert np.all(np.abs(sig.trend) <= 1.0)
    assert np.all((sig.vol_rank >= 0.0) & (sig.vol_rank <= 1.0))
    assert np.all(sig.negative_hours >= 0)


def test_metrics_are_self_consistent(cfg, series):
    r = run_backtest(series, cfg, policy="adaptive")
    assert r.total_return == pytest.approx(r.equity[-1] / r.equity[0] - 1.0)
    assert r.max_drawdown >= 0.0
    assert r.years == pytest.approx(len(series) / (24 * 365))


def test_render_table_covers_every_policy(cfg, series):
    text = render_table(compare(series, cfg))
    for label in ("core only (HODL)", "hedged overlay", "always delta-neutral"):
        assert label in text


def test_rejects_an_unknown_policy(cfg, series):
    with pytest.raises(ValueError):
        run_backtest(series, cfg, policy="yolo")
