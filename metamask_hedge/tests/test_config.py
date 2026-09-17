"""Configuration: merging, normalising, and refusing the impossible."""

import json

import pytest

from mmhedge.config import config_from_dict, load_config, to_dict


def test_weights_normalise_and_uppercase():
    cfg = config_from_dict({"core": {"weights": {"btc": 2.0, "eth": 2.0}}})
    assert cfg.core.weights == {"BTC": 0.5, "ETH": 0.5}


def test_partial_override_keeps_other_defaults():
    cfg = config_from_dict({"hedge": {"leverage": 2.5}})
    assert cfg.hedge.leverage == 2.5
    assert cfg.hedge.ladder_steps == 3


def test_venue_overrides_apply_without_mutating_the_default():
    cfg = config_from_dict({"venue": {"swap_fee_pct": 0.001}})
    assert cfg.venue.swap_fee_pct == 0.001
    assert config_from_dict().venue.swap_fee_pct > 0.001


def test_underscore_keys_are_treated_as_comments():
    cfg = config_from_dict({"_note": "anything at all", "capital_usd": 1_000.0})
    assert cfg.capital_usd == 1_000.0


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        config_from_dict({"hedge": {"levrage": 2.0}})


@pytest.mark.parametrize("patch", [
    {"hedge": {"leverage": 0.0}},
    {"hedge": {"leverage": 60.0}},                      # above the venue ceiling
    {"hedge": {"min_hedge_ratio": 0.8, "max_hedge_ratio": 0.5}},
    {"hedge": {"ladder_steps": 0}},
    {"hedge": {"combine": "vibes"}},
    {"core": {"rebalance_with": "hope"}},
    {"risk": {"survive_rally_pct": 0.0}},
    {"risk": {"margin_call_at": 0.9, "margin_urgent_at": 0.5}},
    {"risk": {"margin_last_resort": "pray"}},
    {"carry": {"exit_apr": 0.5, "enter_apr": 0.2}},     # would thrash the hedge
    {"carry": {"full_apr": 0.1, "enter_apr": 0.2}},
    {"core": {"weights": {}}},
])
def test_invalid_configs_are_refused(patch):
    with pytest.raises(ValueError):
        config_from_dict(patch)


def test_alt_core_forces_lower_leverage():
    # A long-tail perp caps at 10x, so 20x must be refused even though a major
    # would allow it.
    with pytest.raises(ValueError):
        config_from_dict({"core": {"weights": {"SOMECOIN": 1.0}},
                          "hedge": {"leverage": 20.0}})


def test_hedge_symbol_override_routes_to_a_proxy():
    cfg = config_from_dict({
        "core": {"weights": {"WBTC": 1.0}},
        "hedge": {"hedge_symbol_overrides": {"WBTC": "BTC"}}})
    assert cfg.hedge_symbol("WBTC") == "BTC"
    assert cfg.hedged_symbols() == {"WBTC": "BTC"}


def test_blended_maintenance_is_weighted():
    cfg = config_from_dict({"core": {"weights": {"BTC": 0.5, "SOL": 0.5}}})
    assert cfg.blended_maintenance_margin() == pytest.approx((0.01 + 0.025) / 2)


def test_round_trips_through_json(tmp_path):
    cfg = config_from_dict({"capital_usd": 12_345.0, "hedge": {"leverage": 2.5}})
    p = tmp_path / "c.json"
    data = to_dict(cfg)
    data.pop("venue")          # venue is reconstructed from its own defaults
    p.write_text(json.dumps(data))
    reloaded = load_config(p)
    assert reloaded.capital_usd == 12_345.0
    assert reloaded.hedge.leverage == 2.5


@pytest.mark.parametrize("name", ["conservative", "balanced", "aggressive"])
def test_bundled_presets_load_and_validate(name):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "configs" / f"{name}.json")
    cfg.validate()
    assert cfg.name.startswith("metamask-hedged-core")
