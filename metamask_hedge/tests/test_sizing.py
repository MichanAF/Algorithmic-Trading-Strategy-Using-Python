"""The split: derived from the hedge policy, and adding up."""

import pytest

from mmhedge.config import config_from_dict
from mmhedge.sizing import (allocate, leverage_for_survival, liquidation_move,
                            margin_fraction_for_survival)


def test_buckets_sum_to_capital(alloc):
    total = alloc.core_usd + alloc.posted_usd + alloc.reserve_usd + alloc.cash_usd
    assert total == pytest.approx(alloc.capital_usd)


def test_staked_is_part_of_core_not_a_fourth_bucket(alloc):
    assert alloc.staked_usd <= alloc.core_usd
    assert alloc.liquid_core_usd() == pytest.approx(alloc.core_usd - alloc.staked_usd)


def test_collateral_covers_the_rally_it_claims_to(cfg, alloc):
    m = cfg.blended_maintenance_margin()
    needed = margin_fraction_for_survival(cfg.risk.survive_rally_pct, m)
    notional = alloc.hedge_notional(cfg.hedge.max_hedge_ratio)
    assert alloc.posted_usd + alloc.reserve_usd == pytest.approx(
        needed * notional, rel=1e-6)


def test_margin_and_liquidation_are_inverses():
    for move in (0.25, 0.50, 0.80):
        lev = leverage_for_survival(move, 0.01)
        assert liquidation_move(lev, 0.01) == pytest.approx(move)


def test_demanding_a_bigger_rally_shrinks_the_core():
    prev = None
    for survive in (0.25, 0.50, 0.75, 1.00):
        cfg = config_from_dict({
            "capital_usd": 25_000.0,
            "hedge": {"leverage": 1.2},
            "risk": {"survive_rally_pct": survive}})
        core = allocate(cfg).core_usd
        if prev is not None:
            assert core < prev
        prev = core


def test_cash_floor_is_respected():
    cfg = config_from_dict({"capital_usd": 10_000.0})
    a = allocate(cfg, cash_floor_pct=0.25)
    assert a.cash_usd >= 0.25 * a.capital_usd - 1e-6


def test_over_collateralised_leverage_leaves_no_reserve():
    # 3x posts 33.3%; surviving +20% on a major needs 21.2%. Nothing to stage.
    cfg = config_from_dict({
        "capital_usd": 25_000.0, "core": {"weights": {"BTC": 1.0}},
        "hedge": {"leverage": 3.0}, "risk": {"survive_rally_pct": 0.20}})
    a = allocate(cfg)
    assert a.reserve_usd == pytest.approx(0.0)
    assert any("over-collateralised" in n for n in a.notes)


def test_under_collateralised_leverage_stages_a_reserve():
    cfg = config_from_dict({
        "capital_usd": 25_000.0, "core": {"weights": {"BTC": 1.0}},
        "hedge": {"leverage": 3.0}, "risk": {"survive_rally_pct": 0.60}})
    a = allocate(cfg)
    assert a.reserve_usd > 0.0


def test_net_delta_falls_linearly_with_the_hedge(alloc):
    assert alloc.net_delta_usd(0.0) == pytest.approx(alloc.core_usd)
    assert alloc.net_delta_usd(1.0) == pytest.approx(0.0)
    assert alloc.net_delta_usd(0.5) == pytest.approx(alloc.core_usd * 0.5)


def test_riskier_tier_demands_more_collateral_per_dollar_hedged():
    btc = config_from_dict({"capital_usd": 10_000.0,
                            "core": {"weights": {"BTC": 1.0}, "staked_pct": {}}})
    alt = config_from_dict({"capital_usd": 10_000.0,
                            "core": {"weights": {"PEPE": 1.0}, "staked_pct": {}},
                            "hedge": {"leverage": 2.0}})
    assert allocate(alt).blended_margin_fraction > allocate(btc).blended_margin_fraction
    assert allocate(alt).core_usd < allocate(btc).core_usd


def test_concentration_note_fires():
    cfg = config_from_dict({"capital_usd": 10_000.0,
                            "core": {"weights": {"BTC": 0.9, "ETH": 0.1}}})
    assert any("concentration limit" in n for n in allocate(cfg).notes)


def test_small_accounts_are_warned_about_flat_fees():
    cfg = config_from_dict({"capital_usd": 150.0})
    assert any("withdrawal fee" in n for n in allocate(cfg).notes)


def test_yield_excludes_price_and_counts_only_what_earns(cfg, alloc):
    y = alloc.expected_annual_yield(cfg.venue, 0.20, 0.5)
    assert y["funding"] == pytest.approx(alloc.hedge_notional(0.5) * 0.20)
    # The Arbitrum reserve earns nothing; that is the price of being reachable.
    assert y["musd_reserve"] == 0.0
    assert y["total"] == pytest.approx(
        y["funding"] + y["musd_cash"] + y["musd_reserve"] + y["eth_staking"])


def test_rejects_bad_capital_and_floor(cfg):
    with pytest.raises(ValueError):
        allocate(cfg, -1.0)
    with pytest.raises(ValueError):
        allocate(cfg, 10_000.0, cash_floor_pct=1.5)
