"""Per-gate and per-stack edge measurement."""

import pytest

from btc5m.attribution import (EdgeStats, compare_stacks, gate_edge,
                               render_table, stack_edge)
from btc5m.config import config_from_dict
from btc5m.data import synthetic


@pytest.fixture(scope="module")
def series():
    return synthetic(20_000, seed=707)


def test_edge_stats_arithmetic():
    s = EdgeStats("x", signals=100, wins=60, losses=40, voids=0,
                  break_even=0.5, days=10.0)
    assert s.graded == 100
    assert s.accuracy == pytest.approx(0.60)
    assert s.edge == pytest.approx(0.10)
    assert s.stderr == pytest.approx((0.6 * 0.4 / 100) ** 0.5)
    assert s.z == pytest.approx(0.10 / s.stderr)
    assert s.per_day == pytest.approx(10.0)
    assert s.verdict == "edge, significant"


def test_edge_stats_handles_no_signals():
    s = EdgeStats("x", signals=0, wins=0, losses=0, voids=0,
                  break_even=0.5, days=10.0)
    assert s.accuracy is None
    assert s.edge is None
    assert s.z is None
    assert s.verdict == "too few signals to judge"


def test_a_small_sample_is_never_called_an_edge():
    s = EdgeStats("x", signals=20, wins=20, losses=0, voids=0,
                  break_even=0.5, days=1.0)
    assert s.verdict == "too few signals to judge"


def test_a_significantly_negative_gate_is_labelled_as_such():
    s = EdgeStats("x", signals=1000, wins=400, losses=600, voids=0,
                  break_even=0.5, days=10.0)
    assert s.verdict == "negative edge, significant"


def test_voids_are_excluded_from_the_hit_rate():
    s = EdgeStats("x", signals=100, wins=50, losses=30, voids=20,
                  break_even=0.5, days=1.0)
    assert s.graded == 80
    assert s.accuracy == pytest.approx(50 / 80)


def test_gate_edge_covers_every_non_veto_gate(series):
    cfg = config_from_dict()
    stats = gate_edge(series, cfg)
    names = {s.label for s in stats}
    assert {"trend_alignment", "persistence", "participation",
            "momentum_thrust", "mean_reversion", "location",
            "cross_asset"} <= names
    assert "data_integrity" not in names          # veto gates have no direction
    assert "volatility_regime" not in names


def test_gate_edge_reports_nothing_for_a_gate_that_cannot_run(series):
    stats = {s.label: s for s in gate_edge(series, config_from_dict())}
    assert stats["cross_asset"].signals == 0     # no reference series supplied


def test_stack_edge_counts_only_tradable_signals(series):
    cfg = config_from_dict()
    stats = stack_edge(series, cfg, label="default")
    assert stats.label == "default"
    assert stats.signals == stats.wins + stats.losses + stats.voids
    assert stats.days == pytest.approx(len(series) / 288)


def test_a_looser_stack_produces_more_signals(series):
    tight = config_from_dict({"betting": {"min_conviction": 0.8}})
    loose = config_from_dict({"betting": {"min_conviction": 0.0,
                                          "required_edge": -1.0}})
    assert (stack_edge(series, loose).signals
            > stack_edge(series, tight).signals)


def test_compare_stacks_scores_each_candidate(series):
    stacks = {
        "three": ["data_integrity", "volatility_regime", "trend_alignment"],
        "five": ["data_integrity", "volatility_regime", "trend_alignment",
                 "persistence", "participation"],
    }
    stats = compare_stacks(series, stacks)
    assert {s.label for s in stats} == {"three", "five"}
    by_label = {s.label: s for s in stats}
    # Adding gates can only narrow the set of bars that pass.
    assert by_label["five"].signals <= by_label["three"].signals


def test_compare_stacks_clamps_the_directional_minimum(series):
    """A three-gate stack has one directional gate; comparing must not error."""
    stats = compare_stacks(
        series,
        {"one_directional": ["data_integrity", "volatility_regime",
                             "trend_alignment"]},
        base={"betting": {"min_directional_gates": 3}})
    assert stats[0].signals > 0


def test_compare_stacks_does_not_mutate_the_base(series):
    base = {"betting": {"min_directional_gates": 2}}
    compare_stacks(series, {"a": ["data_integrity", "volatility_regime",
                                  "trend_alignment"]}, base=base)
    assert base == {"betting": {"min_directional_gates": 2}}


def test_render_table_is_readable(series):
    stats = gate_edge(series, config_from_dict())
    text = render_table(stats, "title")
    assert "title" in text
    assert "break-even to beat" in text
    for s in stats:
        assert s.label in text
