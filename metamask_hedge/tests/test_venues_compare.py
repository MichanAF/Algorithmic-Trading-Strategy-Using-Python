"""Two venues, and the meme tier. The margin model beats the fee schedule."""

import pytest

from mmhedge.config import config_from_dict
from mmhedge.sizing import allocate
from mmhedge.venue import (METAMASK, OKX, TIERS, VENUES, Venue, compare_venues)
from mmhedge.viability import OperatingCosts, assess_viability

BTC_ONLY = {"core": {"weights": {"BTC": 1.0}, "staked_pct": {}}}


# -- the fee schedules point in opposite directions -------------------------- #

def test_okx_spot_is_far_cheaper_metamask_perp_is_cheaper():
    # Neither venue wins on fees outright, which is why the margin model
    # decides it rather than the fee table.
    assert OKX.spot_cost_pct() < METAMASK.spot_cost_pct() / 5
    assert METAMASK.perp_cost_pct() < OKX.perp_cost_pct()
    assert METAMASK.perp_cost_pct(maker=True) < 0.0     # rebate
    assert OKX.perp_cost_pct(maker=True) > 0.0          # cost


def test_neither_venue_is_scored_with_a_free_cash_yield():
    # Zeroing a venue's cash yield silently wins it every opportunity-cost
    # comparison in the package.
    assert METAMASK.musd_apy > 0.0
    assert OKX.musd_apy > 0.0


# -- the margin model ------------------------------------------------------- #

def test_cross_margin_makes_a_major_pair_effectively_unliquidatable():
    assert OKX.hedged_liquidation_move("BTC", 2.0) > 5.0        # >+500%
    assert METAMASK.hedged_liquidation_move("BTC", 2.0) < 0.6   # <+60%


def test_isolated_hedged_liquidation_is_just_the_naked_short():
    assert (METAMASK.hedged_liquidation_move("BTC", 3.0)
            == pytest.approx(METAMASK.liquidation_move("BTC", 3.0)))


def test_a_meme_pair_is_still_liquidatable_under_cross_margin():
    # The haircut is what bites, not the maintenance margin.
    move = OKX.hedged_liquidation_move("PEPE", 2.0)
    assert 0.5 < move < 3.0
    assert move > METAMASK.hedged_liquidation_move("PEPE", 2.0)


def test_a_token_refused_as_collateral_falls_back_to_isolated():
    refused = OKX.with_overrides(collateral_haircut=1.0)
    assert (refused.hedged_liquidation_move("BTC", 2.0)
            == pytest.approx(refused.liquidation_move("BTC", 2.0)))


def test_deeper_haircuts_liquidate_sooner():
    prev = None
    for sym in ("BTC", "SOL", "SOMECOIN", "PEPE"):
        move = OKX.hedged_liquidation_move(sym, 2.0)
        if prev is not None:
            assert move <= prev
        prev = move


# -- collateral required ----------------------------------------------------- #

def test_cross_margin_asks_for_nothing_extra_on_a_major():
    assert OKX.hedge_collateral_fraction("BTC", 0.50) == 0.0
    assert METAMASK.hedge_collateral_fraction("BTC", 0.50) == pytest.approx(0.515, abs=1e-3)


def test_a_meme_at_a_big_target_needs_real_collateral_even_on_okx():
    assert OKX.hedge_collateral_fraction("PEPE", 2.00) == pytest.approx(0.35, abs=1e-3)
    # Isolated, the same target needs more collateral than the position is worth.
    assert METAMASK.hedge_collateral_fraction("PEPE", 2.00) > 1.0


def test_cross_margin_frees_capital_into_the_core():
    mm = allocate(config_from_dict({"capital_usd": 25_000.0, **BTC_ONLY}))
    okx = allocate(config_from_dict({"capital_usd": 25_000.0, "venue": "okx",
                                     **BTC_ONLY}))
    assert okx.core_usd > mm.core_usd * 1.4
    assert okx.posted_usd == pytest.approx(0.0)
    assert okx.reserve_usd == pytest.approx(0.0)


# -- viability --------------------------------------------------------------- #

def test_okx_clears_the_fixed_cost_barrier_at_a_size_metamask_cannot():
    okx_costs = OperatingCosts(gas_per_tx_usd=0.0, withdrawals_per_year=0)
    okx = assess_viability(config_from_dict({"venue": "okx", **BTC_ONLY}),
                           200.0, costs=okx_costs)
    mm = assess_viability(config_from_dict(BTC_ONLY), 200.0)
    assert okx.viable and not mm.viable
    assert okx.minimum_usd < mm.minimum_usd / 10


# -- config plumbing --------------------------------------------------------- #

def test_venue_selectable_by_name():
    assert config_from_dict({"venue": "okx"}).venue.name == "okx"
    assert config_from_dict({}).venue.name == "metamask"


def test_preset_plus_overrides():
    cfg = config_from_dict({"venue": {"preset": "okx", "perp_taker_pct": 0.0001}})
    assert cfg.venue.name == "okx"
    assert cfg.venue.perp_taker_pct == 0.0001
    assert cfg.venue.cross_margin is True
    assert OKX.perp_taker_pct != 0.0001          # preset itself untouched


def test_unknown_venue_is_refused():
    with pytest.raises(ValueError):
        config_from_dict({"venue": "ftx"})


def test_meme_tier_is_flagged_unverified():
    assert TIERS["meme"].verified is False
    assert TIERS["meme"].collateral_haircut > TIERS["major"].collateral_haircut


def test_meme_symbols_route_to_the_meme_tier():
    for sym in ("PEPE", "SHIB", "BONK", "WIF", "FLOKI"):
        assert METAMASK.tier_for(sym).name == "meme"


def test_compare_renders_both_venues():
    text = compare_venues(METAMASK, OKX, "BTC", 2.0)
    assert "metamask" in text and "okx" in text
    assert "isolated only" in text and "cross" in text


def test_venues_registry_is_complete():
    assert set(VENUES) == {"metamask", "okx"}
    assert all(isinstance(v, Venue) for v in VENUES.values())
