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


def test_end_date_is_midnight_utc_on_that_day():
    from datetime import datetime, timezone
    from btc5m.cli import _parse_end

    assert _parse_end("2024-09-13") == datetime(2024, 9, 13, tzinfo=timezone.utc)
    with pytest.raises(SystemExit, match="YYYY-MM-DD"):
        _parse_end("13/09/2024")


def test_end_date_is_refused_on_the_single_request_path(capsys):
    """The one-shot REST call always returns the most recent bars, so --end
    would be silently ignored there.  Refuse rather than mislead."""
    code, out = run(["fetch", "--end", "2024-09-13", "--source", "api"], capsys)
    assert code == 2
    assert "--limit above 1000" in out.err


def test_end_date_reaches_the_archive_fetch(monkeypatch, tmp_path, capsys):
    """--year --end routes to the archives with the date as the series end."""
    from datetime import datetime, timezone
    import btc5m.cli as cli
    from btc5m.data import synthetic

    seen = {}

    def fake_dump(symbol, bars, pause_seconds=0.0, now=None, progress=None,
                  interval="5m"):
        seen.update(symbol=symbol, bars=bars, now=now, interval=interval)
        return synthetic(400, seed=1)

    monkeypatch.setattr(cli, "fetch_binance_dump", fake_dump)
    out_path = tmp_path / "dev.csv"
    code, out = run(["fetch", "--year", "--end", "2024-09-13",
                     "-o", str(out_path)], capsys)
    assert code == 0
    assert seen["now"] == datetime(2024, 9, 13, tzinfo=timezone.utc)
    assert seen["bars"] == 105_120
    assert seen["interval"] == "5m"
    assert out_path.exists()


def test_the_fade_config_loads_and_pins_its_hypothesis():
    """The hypothesis under test must not drift with library defaults."""
    from btc5m.config import load_config

    cfg = load_config("configs/fade-5m.json")
    cfg.validate()
    assert cfg.gate_stack == ["data_integrity", "mean_reversion", "session"]
    assert cfg.betting.min_directional_gates == 1
    # session is the validator's third gate, not a filter: every hour open,
    # weekends open, so it cannot quietly become part of the hypothesis.
    assert cfg.gates.session.allowed_hours_utc == list(range(24))
    assert cfg.gates.session.skip_weekend is False
    # Pinned explicitly, so a defaults change cannot alter the test.
    assert cfg.gates.mean_reversion.min_abs_z == pytest.approx(1.8)
    assert cfg.gates.mean_reversion.max_variance_ratio == pytest.approx(1.0)
    # Same venue economics as the strategy it replaces, so the comparison is fair.
    assert cfg.venue == "predict-fun-btc-5m"
    assert cfg.break_even_probability() == pytest.approx(0.52)


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


def test_a_year_of_minute_bars_is_525600_of_them(monkeypatch, tmp_path, capsys):
    import btc5m.cli as cli
    from btc5m.data import synthetic

    seen = {}

    def fake_dump(symbol, bars, pause_seconds=0.0, now=None, progress=None,
                  interval="5m"):
        seen.update(bars=bars, interval=interval)
        return synthetic(400, seed=1)

    monkeypatch.setattr(cli, "fetch_binance_dump", fake_dump)
    code, out = run(["fetch", "--interval", "1m", "--year",
                     "-o", str(tmp_path / "minutes.csv")], capsys)
    assert code == 0
    assert seen == {"bars": 525_600, "interval": "1m"}
    assert "closed 1m bars" in out.out


def test_minute_candles_are_refused_off_binance_before_any_request(capsys):
    code, out = run(["fetch", "--exchange", "coinbase", "--interval", "1m",
                     "--limit", "10", "--source", "api"], capsys)
    assert code == 2
    assert "5m candles only" in out.err


def test_the_fade_flow_config_loads_and_pins_its_decisions():
    """The second pre-registered config: the fade gate untouched, the taker
    gate at the values chosen on the development year, either gate free to
    carry a bar, the taker vote flat."""
    import pytest
    from btc5m.config import load_config, to_dict

    cfg = load_config("configs/fade-flow-5m.json")
    fade = load_config("configs/fade-5m.json")
    assert cfg.gate_stack == ["data_integrity", "mean_reversion", "taker_flow",
                              "session"]
    tf = cfg.gates.taker_flow
    assert (tf.mode, tf.source, tf.window, tf.z_window) == ("fade", "5m", 1, 288)
    assert (tf.min_abs_z, tf.min_volume_ratio, tf.score_span) == (2.0, 0.0, 0.0)
    assert cfg.betting.mode == "weighted"
    assert cfg.betting.allow_dissent is False
    assert cfg.betting.min_conviction == fade.betting.min_conviction == 0.85
    assert to_dict(cfg.gates.mean_reversion) == to_dict(fade.gates.mean_reversion)
    assert to_dict(cfg.gates.session) == to_dict(fade.gates.session)
    assert to_dict(cfg.risk) == to_dict(fade.risk)
    assert cfg.break_even_probability() == pytest.approx(0.52)
    assert "2023-09-13" in cfg_text() and "+1.0%" in cfg_text()


def cfg_text() -> str:
    from pathlib import Path
    return Path("configs/fade-flow-5m.json").read_text()
