"""Every subcommand runs, and the overrides reach the config."""

from pathlib import Path

import pytest

from mmhedge.cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("argv", [
    ["venue"],
    ["plan", "--capital", "25000"],
    ["plan", "--capital", "25000", "--funding", "0.3", "--hedge", "0.5"],
    ["carry", "--funding", "0.25"],
    ["carry", "--funding", "0.25", "--taker", "--notional", "5000"],
    ["hedge", "--funding", "0.3", "--trend", "-0.4"],
    ["hedge", "--funding", "0.3", "--cooldown", "--current", "0.2"],
    ["risk", "--capital", "25000", "--hedge", "1.0", "--price", "95000"],
    ["backtest", "--synthetic", "2400", "--capital", "25000"],
    ["backtest", "--synthetic", "2400", "--policy", "neutral"],
    ["compare", "--synthetic", "2400", "--capital", "25000"],
    ["sweep", "--seeds", "2", "--hours", "2400"],
    ["config"],
    ["viability", "--capital", "200"],
    ["viability", "--capital", "25000", "--funding", "0.3"],
    ["viability", "--capital", "200", "--adding", "1200", "--gas", "0.1"],
])
def test_subcommands_exit_clean(argv, capsys):
    assert main(argv) == 0
    assert capsys.readouterr().out.strip()


def test_set_override_reaches_the_config(capsys):
    main(["config", "--set", "hedge.leverage=2.75"])
    assert '"leverage": 2.75' in capsys.readouterr().out


def test_bool_and_int_overrides_are_coerced(capsys):
    main(["config", "--set", "hedge.use_maker_orders=false",
          "--set", "hedge.ladder_steps=5"])
    out = capsys.readouterr().out
    assert '"use_maker_orders": false' in out
    assert '"ladder_steps": 5' in out


@pytest.mark.parametrize("name", ["conservative", "balanced", "aggressive"])
def test_presets_drive_the_plan(name, capsys):
    assert main(["plan", "--config", str(ROOT / "configs" / f"{name}.json")]) == 0
    assert "allocation on" in capsys.readouterr().out


def test_unknown_key_is_a_clean_error():
    with pytest.raises(SystemExit):
        main(["config", "--set", "hedge.nonsense=1"])


def test_malformed_set_is_a_clean_error():
    with pytest.raises(SystemExit):
        main(["config", "--set", "no-equals-sign"])


def test_invalid_value_reports_rather_than_traces(capsys):
    assert main(["plan", "--set", "hedge.leverage=999"]) == 2
    assert "error:" in capsys.readouterr().err


def test_leverage_flag_changes_the_split(capsys):
    main(["plan", "--capital", "25000", "--leverage", "3.0"])
    assert "3x on the short" in capsys.readouterr().out


def test_viability_says_no_at_two_hundred(capsys):
    assert main(["viability", "--capital", "200"]) == 0
    out = capsys.readouterr().out
    assert "NOT VIABLE" in out
    assert "the overlay needs about" in out


def test_viability_says_yes_at_twenty_five_thousand(capsys):
    assert main(["viability", "--capital", "25000"]) == 0
    assert "=> VIABLE" in capsys.readouterr().out


def test_viability_prints_cadence_costs_when_asked(capsys):
    assert main(["viability", "--capital", "200", "--adding", "1200"]) == 0
    out = capsys.readouterr().out
    assert "monthly" in out and "quarterly" in out
