"""Whether the account is big enough, and what adding to it costs."""

import pytest

from mmhedge.config import config_from_dict
from mmhedge.viability import (OperatingCosts, assess_viability, dca_drag,
                               dca_table, ramp_table)


def blocked(report, label):
    found = [c for c in report.checks if c.label == label]
    assert found, f"no check {label!r}"
    return not found[0].passed


def test_two_hundred_dollars_is_not_viable(cfg):
    v = assess_viability(cfg, 200.0)
    assert not v.viable
    assert v.net_usd < 0.0
    assert blocked(v, "net_carry_positive")
    assert blocked(v, "overhead_share")
    # The mechanical failure is the decisive one: the policy cannot place the
    # trade it decided on, whatever the economics say.
    assert blocked(v, "clip_is_tradeable")


def test_a_real_account_is_viable(cfg):
    v = assess_viability(cfg, 25_000.0)
    assert v.viable
    assert v.net_usd > 0.0
    assert v.overhead_share < 0.05


def test_fixed_costs_do_not_shrink_with_the_account(cfg):
    small = assess_viability(cfg, 200.0)
    large = assess_viability(cfg, 25_000.0)
    assert small.fixed_costs_usd == pytest.approx(large.fixed_costs_usd)
    # but they go from dominating to irrelevant
    assert small.overhead_share > 1.0
    assert large.overhead_share < 0.05


def test_net_carry_rises_monotonically_with_size(cfg):
    prev = None
    for cap in (200, 500, 1_000, 5_000, 25_000):
        net = assess_viability(cfg, float(cap)).net_usd
        if prev is not None:
            assert net > prev
        prev = net


def test_net_percentage_converges_once_gas_is_amortised(cfg):
    mid = assess_viability(cfg, 10_000.0).net_pct
    big = assess_viability(cfg, 100_000.0).net_pct
    assert big > mid
    assert abs(big - mid) < 0.01          # converging, not still climbing


def test_the_threshold_is_the_worse_of_two_constraints(cfg):
    v = assess_viability(cfg, 200.0)
    assert v.minimum_usd == max(v.economic_minimum_usd, v.mechanical_minimum_usd)
    assert v.minimum_usd > 200.0


def test_expensive_gas_raises_the_threshold(cfg):
    cheap = assess_viability(cfg, 2_000.0, costs=OperatingCosts(gas_per_tx_usd=0.10))
    costly = assess_viability(cfg, 2_000.0, costs=OperatingCosts(gas_per_tx_usd=8.0))
    assert costly.economic_minimum_usd > cheap.economic_minimum_usd
    assert costly.net_usd < cheap.net_usd


def test_richer_funding_lowers_the_threshold(cfg):
    lean = assess_viability(cfg, 2_000.0, funding_apr=0.08)
    rich = assess_viability(cfg, 2_000.0, funding_apr=0.40)
    assert rich.economic_minimum_usd < lean.economic_minimum_usd


def test_a_single_asset_core_trades_at_smaller_size():
    # Three assets means the smallest slice sets the mechanical floor; one
    # asset removes that constraint entirely.
    three = config_from_dict({"core": {"weights": {"BTC": 0.55, "ETH": 0.30,
                                                   "SOL": 0.15}}})
    one = config_from_dict({"core": {"weights": {"BTC": 1.0}, "staked_pct": {}}})
    assert (assess_viability(one, 1_000.0).mechanical_minimum_usd
            < assess_viability(three, 1_000.0).mechanical_minimum_usd)


def test_collateral_drag_is_charged_against_musd(cfg):
    v = assess_viability(cfg, 25_000.0)
    assert v.collateral_drag_usd == pytest.approx(
        v.collateral_usd * cfg.venue.musd_apy)


def test_rejects_bad_capital(cfg):
    with pytest.raises(ValueError):
        assess_viability(cfg, 0.0)


# -- adding to the core ----------------------------------------------------- #

def test_swap_fee_is_cadence_blind_but_gas_is_not(cfg):
    monthly = dca_drag(cfg.venue, 100.0, per_year=12)
    quarterly = dca_drag(cfg.venue, 300.0, per_year=4)
    assert monthly["annual_contributed_usd"] == quarterly["annual_contributed_usd"]
    assert monthly["annual_cost_usd"] > quarterly["annual_cost_usd"]
    # And the whole difference is gas.
    assert (monthly["annual_cost_usd"] - quarterly["annual_cost_usd"]
            == pytest.approx(8 * 0.50))


def test_gas_share_falls_as_contributions_grow(cfg):
    small = dca_drag(cfg.venue, 50.0, per_year=12)
    large = dca_drag(cfg.venue, 2_000.0, per_year=12)
    assert small["gas_share"] > large["gas_share"]


def test_dca_drag_rejects_nonsense(cfg):
    with pytest.raises(ValueError):
        dca_drag(cfg.venue, 0.0)
    with pytest.raises(ValueError):
        dca_drag(cfg.venue, 100.0, per_year=0)


def test_tables_render(cfg):
    ramp = ramp_table(cfg)
    assert "viable" in ramp and "capital" in ramp
    dca = dca_table(cfg.venue, 1_200.0)
    assert "monthly" in dca and "quarterly" in dca
