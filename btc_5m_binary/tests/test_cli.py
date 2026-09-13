"""Command-line surface: every subcommand runs and reports something useful."""

import json

import pytest

from btc5m.cli import PRESETS, _build_config, _coerce, build_parser, main
from btc5m.config import config_from_dict


def run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr()


def test_every_preset_is_a_valid_config():
    for name, stack in PRESETS.items():
        cfg = config_from_dict({"gate_stack": stack,
                                "betting": {"min_directional_gates": 1}})
        cfg.validate()
        assert cfg.gate_stack == stack, name


def test_menu_lists_every_gate_and_preset(capsys):
    code, out = run(["menu"], capsys)
    assert code == 0
    from btc5m.gates import GATE_REGISTRY
    for name in GATE_REGISTRY:
        assert name in out.out
    for name in PRESETS:
        assert name in out.out


def test_signal_prints_a_full_gate_trace(capsys):
    code, out = run(["signal", "--synthetic", "3000", "--bar", "2500"], capsys)
    assert code == 0
    assert "gates:" in out.out
    assert "betting conditions:" in out.out
    assert "ANSWER:" in out.out
    assert "data_integrity" in out.out


def test_backtest_prints_a_summary_and_writes_json(tmp_path, capsys):
    target = tmp_path / "r.json"
    code, out = run(["backtest", "--synthetic", "8000", "--json", str(target)],
                    capsys)
    assert code == 0
    assert "BACKTEST" in out.out
    payload = json.loads(target.read_text())
    assert "config" in payload and "equity" in payload
    assert payload["break_even"] > 0.5


def test_gates_command_scores_each_gate(capsys):
    code, out = run(["gates", "--synthetic", "6000"], capsys)
    assert code == 0
    assert "trend_alignment" in out.out
    assert "break-even to beat" in out.out


def test_compare_command_scores_presets(capsys):
    code, out = run(["compare", "--synthetic", "6000",
                     "--presets", "core3", "default"], capsys)
    assert code == 0
    assert "core3" in out.out and "default" in out.out


def test_sample_writes_a_loadable_csv(tmp_path, capsys):
    target = tmp_path / "bars.csv"
    code, out = run(["sample", "--bars", "1000", "-o", str(target)], capsys)
    assert code == 0
    from btc5m.data import load_csv
    assert len(load_csv(target)) == 1000


def test_config_command_emits_valid_json(capsys):
    code, out = run(["config"], capsys)
    assert code == 0
    payload = json.loads(out.out)
    assert payload["gate_stack"]


def test_preset_and_explicit_gates_are_applied():
    parser = build_parser()
    args = parser.parse_args(["config", "--preset", "core3"])
    assert _build_config(args).gate_stack == PRESETS["core3"]

    args = parser.parse_args(["config", "--gates",
                              "data_integrity,volatility_regime,trend_alignment"])
    assert _build_config(args).gate_stack == ["data_integrity",
                                              "volatility_regime",
                                              "trend_alignment"]


def test_set_overrides_nested_values():
    args = build_parser().parse_args(
        ["config", "--set", "betting.net_payout=0.95",
         "--set", "risk.kelly_fraction=0.4",
         "--set", "gates.trend_alignment.require_vwap_side=false"])
    cfg = _build_config(args)
    assert cfg.betting.net_payout == pytest.approx(0.95)
    assert cfg.risk.kelly_fraction == pytest.approx(0.4)
    assert cfg.gates.trend_alignment.require_vwap_side is False


def test_set_without_an_equals_sign_is_rejected():
    args = build_parser().parse_args(["config", "--set", "nonsense"])
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        _build_config(args)


def test_coerce_handles_the_usual_literals():
    assert _coerce("true") is True
    assert _coerce("false") is False
    assert _coerce("none") is None
    assert _coerce("7") == 7
    assert _coerce("0.5") == pytest.approx(0.5)
    assert _coerce("[1, 2]") == [1, 2]
    assert _coerce("a,b") == ["a", "b"]
    assert _coerce("hello") == "hello"


def test_missing_data_source_explains_the_options():
    with pytest.raises(SystemExit, match="no data source"):
        main(["backtest"])


def test_two_data_sources_are_rejected():
    with pytest.raises(SystemExit, match="pick one data source"):
        main(["backtest", "--synthetic", "2000", "--data", "nope.csv"])


def test_too_little_synthetic_data_is_refused():
    with pytest.raises(SystemExit, match="warm-up"):
        main(["backtest", "--synthetic", "100"])


def test_a_bar_inside_the_warmup_is_refused():
    with pytest.raises(SystemExit, match="warm-up"):
        main(["signal", "--synthetic", "3000", "--bar", "5"])


def test_an_out_of_range_bar_is_refused():
    with pytest.raises(SystemExit, match="outside"):
        main(["signal", "--synthetic", "3000", "--bar", "99999"])


def test_a_negative_bar_counts_from_the_end(capsys):
    code, _ = run(["signal", "--synthetic", "3000", "--bar", "-1"], capsys)
    assert code == 0


def test_a_missing_csv_is_reported_not_raised(capsys):
    code, out = run(["backtest", "--data", "does_not_exist.csv"], capsys)
    assert code == 2
    assert "error:" in out.err


def test_an_invalid_gate_name_is_reported(capsys):
    code, out = run(["backtest", "--synthetic", "2000", "--gates", "nope,also_nope"],
                    capsys)
    assert code == 2
    assert "error:" in out.err


def test_synthetic_runs_carry_a_warning(capsys):
    _, out = run(["gates", "--synthetic", "3000"], capsys)
    assert "synthetic" in out.err.lower()
