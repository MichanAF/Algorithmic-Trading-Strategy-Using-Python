"""Config merging, validation, and the payout arithmetic everything rests on."""

import json

import pytest

from btc5m.config import (DEFAULT_GATE_STACK, config_from_dict, load_config,
                          to_dict)


def test_defaults_are_valid_and_five_gates_deep():
    cfg = config_from_dict()
    cfg.validate()
    assert cfg.gate_stack == list(DEFAULT_GATE_STACK)
    assert len(cfg.gate_stack) == 5


def test_overrides_merge_without_clobbering_siblings():
    cfg = config_from_dict({"gates": {"momentum_thrust": {"min_roc_z": 2.5}}})
    assert cfg.gates.momentum_thrust.min_roc_z == 2.5
    assert cfg.gates.momentum_thrust.rsi_period == 14          # untouched
    assert cfg.gates.trend_alignment.fast == 9                  # untouched


def test_unknown_keys_are_rejected_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown config key"):
        config_from_dict({"betting": {"net_payout_typo": 0.9}})
    with pytest.raises(ValueError, match="unknown config key"):
        config_from_dict({"nonsense": 1})


def test_an_empty_gate_stack_is_rejected():
    with pytest.raises(ValueError, match="never bet"):
        config_from_dict({"gate_stack": []})


def test_unknown_gates_are_rejected():
    with pytest.raises(ValueError, match="unknown gate"):
        config_from_dict({"gate_stack": ["data_integrity", "vibes"]})


def test_duplicate_gates_are_rejected():
    with pytest.raises(ValueError, match="duplicates"):
        config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                         "trend_alignment", "trend_alignment"]})


def test_a_stack_of_only_vetoes_is_rejected():
    with pytest.raises(ValueError, match="no directional gate"):
        config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                         "session"]})


def test_stack_size_is_bounded_to_the_workable_range():
    with pytest.raises(ValueError, match="3-5"):
        config_from_dict({"gate_stack": ["trend_alignment", "persistence"]})
    with pytest.raises(ValueError, match="3-5"):
        config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                         "session", "trend_alignment",
                                         "persistence", "participation",
                                         "momentum_thrust", "location"]})


def test_min_directional_gates_cannot_exceed_the_stack():
    with pytest.raises(ValueError, match="min_directional_gates"):
        config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                         "trend_alignment"],
                          "betting": {"min_directional_gates": 3}})


@pytest.mark.parametrize("overrides,match", [
    ({"betting": {"mode": "vibes"}}, "betting mode"),
    ({"betting": {"payout_mode": "vibes"}}, "payout mode"),
    ({"betting": {"tie_policy": "vibes"}}, "tie policy"),
    ({"betting": {"prob_cap": 1.5}}, "prob_cap"),
    ({"betting": {"horizon_bars": 0}}, "horizon_bars"),
    ({"risk": {"max_stake_pct": 0.0}}, "max_stake_pct"),
    ({"risk": {"kelly_fraction": 0.0}}, "kelly_fraction"),
    ({"risk": {"starting_bankroll": 0.0}}, "starting_bankroll"),
])
def test_invalid_values_are_rejected(overrides, match):
    with pytest.raises(ValueError, match=match):
        config_from_dict(overrides)


# --------------------------------------------------------------------------- #
# payout arithmetic
# --------------------------------------------------------------------------- #

def test_fixed_odds_break_even_is_one_over_one_plus_payout():
    cfg = config_from_dict({"betting": {"net_payout": 1.0}})
    assert cfg.break_even_probability() == pytest.approx(0.5)
    assert cfg.payoff_odds() == pytest.approx(1.0)

    cfg = config_from_dict({"betting": {"net_payout": 0.9}})
    assert cfg.break_even_probability() == pytest.approx(1 / 1.9)


def test_a_break_even_bet_has_zero_expected_value():
    cfg = config_from_dict({"betting": {"net_payout": 0.8}})
    p = cfg.break_even_probability()
    assert p * cfg.payoff_odds() - (1 - p) == pytest.approx(0.0, abs=1e-9)


def test_a_payout_that_cannot_cover_fees_is_rejected():
    """Caught when the config is built, not later when a bet is priced."""
    with pytest.raises(ValueError, match="net_payout"):
        config_from_dict({"betting": {"net_payout": 0.001, "fee_bps": 100.0}})


def test_an_out_of_range_contract_price_is_rejected():
    with pytest.raises(ValueError, match="contract_price"):
        config_from_dict({"betting": {"payout_mode": "contract_price",
                                      "contract_price": 0.999, "fee_bps": 500.0}})


def test_a_payout_no_conviction_can_clear_is_rejected():
    """prob_cap below break-even means the strategy could never bet at all."""
    with pytest.raises(ValueError, match="never bet"):
        config_from_dict({"betting": {"net_payout": 0.60, "prob_cap": 0.55}})


def test_implied_conviction_floor_translates_the_edge_requirement():
    cfg = config_from_dict()
    floor = cfg.implied_conviction_floor()
    assert floor is not None
    # At exactly the floor the edge requirement is met; just below it is not.
    from btc5m.signal import SignalEngine
    engine = SignalEngine(cfg)
    at = engine._probability(floor) - cfg.break_even_probability()
    below = engine._probability(floor * 0.95) - cfg.break_even_probability()
    assert at >= cfg.betting.required_edge - 1e-9
    assert below < cfg.betting.required_edge


def test_binding_condition_flips_when_the_payout_worsens():
    """The two conditions are one threshold in two units; the payout picks which."""
    generous = config_from_dict({"betting": {"net_payout": 0.95}})
    stingy = config_from_dict({"betting": {"net_payout": 0.80}})
    assert generous.binding_betting_condition() == "min_conviction"
    assert stingy.binding_betting_condition() == "required_edge"
    assert (stingy.implied_conviction_floor()
            > generous.implied_conviction_floor())


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #

def test_round_trips_through_a_dict():
    original = config_from_dict({"betting": {"net_payout": 0.93},
                                 "risk": {"kelly_fraction": 0.3}})
    restored = config_from_dict(to_dict(original))
    assert to_dict(restored) == to_dict(original)


def test_to_dict_is_json_serialisable():
    json.dumps(to_dict(config_from_dict()))


def test_loads_json_and_yaml_files(tmp_path):
    payload = {"name": "from-file", "betting": {"net_payout": 0.88}}
    j = tmp_path / "c.json"
    j.write_text(json.dumps(payload))
    assert load_config(j).name == "from-file"
    assert load_config(j).betting.net_payout == pytest.approx(0.88)

    yaml = pytest.importorskip("yaml")
    y = tmp_path / "c.yaml"
    y.write_text(yaml.safe_dump(payload))
    assert load_config(y).betting.net_payout == pytest.approx(0.88)


def test_loading_nothing_gives_the_defaults():
    assert load_config(None).gate_stack == list(DEFAULT_GATE_STACK)


def test_a_non_mapping_config_file_is_rejected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="mapping"):
        load_config(p)


def test_an_empty_config_file_gives_the_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("")
    assert load_config(p).gate_stack == list(DEFAULT_GATE_STACK)


def test_bundled_configs_all_load():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "configs"
    files = sorted(root.glob("*.json"))
    assert files, "no bundled configs found"
    for f in files:
        cfg = load_config(f)
        cfg.validate()
        assert 0.0 < cfg.break_even_probability() < 1.0
        assert cfg.payoff_odds() > 0.0
