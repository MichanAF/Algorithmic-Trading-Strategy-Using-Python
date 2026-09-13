"""Overlap analysis: are the gates and conditions asking different questions?"""

import numpy as np
import pytest

from btc5m.config import config_from_dict
from btc5m.data import synthetic
from btc5m.features import build_features
from btc5m.gates import DOWN, FLAT, GATE_REGISTRY, UP
from btc5m.redundancy import (MarginalValue, PairOverlap, collect_verdicts,
                              condition_bindings, full_report, leave_one_out,
                              marginal_value, structural_overlap,
                              verdict_overlap)

STACK = ["data_integrity", "volatility_regime", "trend_alignment",
         "persistence", "participation"]


@pytest.fixture(scope="module")
def rig():
    cfg = config_from_dict()
    series = synthetic(20_000, seed=311)
    fs = build_features(series, cfg)
    return cfg, series, fs, collect_verdicts(series, cfg, STACK, features=fs)


# --------------------------------------------------------------------------- #
# structural
# --------------------------------------------------------------------------- #

def test_the_default_stack_shares_no_features():
    assert structural_overlap(STACK) == []


def test_shared_features_are_found_when_they_exist():
    rows = structural_overlap(["momentum_thrust", "participation"])
    assert len(rows) == 1
    assert rows[0].shared == ("body_dominance",)
    assert 0.0 < rows[0].share_of_a <= 1.0
    assert 0.0 < rows[0].share_of_b <= 1.0


def test_contradictory_gates_are_reported_as_sharing_their_input():
    shared = {r.shared for r in structural_overlap(["persistence", "mean_reversion"])}
    assert ("variance_ratio",) in shared


# --------------------------------------------------------------------------- #
# verdict collection
# --------------------------------------------------------------------------- #

def test_verdicts_are_aligned_and_typed(rig):
    _, series, _, v = rig
    assert v.bars > 0
    assert set(v.names) == set(STACK)
    for name in STACK:
        assert v.passed[name].shape == (v.bars,)
        assert v.passed[name].dtype == bool
        assert v.direction[name].shape == (v.bars,)
        assert set(np.unique(v.direction[name])) <= {UP, FLAT, DOWN}
    assert v.outcome.shape == (v.bars,)
    assert v.proposed.shape == (v.bars,)


def test_a_proposed_side_means_every_directional_gate_agreed(rig):
    _, _, _, v = rig
    directional = [n for n in STACK if GATE_REGISTRY[n].is_directional]
    sided = v.proposed != FLAT
    assert sided.any()
    for name in directional:
        assert np.all(v.passed[name][sided])
        assert np.all(v.direction[name][sided] == v.proposed[sided])


def test_veto_gates_never_carry_a_direction(rig):
    _, _, _, v = rig
    for name in STACK:
        if GATE_REGISTRY[name].is_veto:
            assert np.all(v.direction[name] == FLAT)


def test_confirmation_gates_are_judged_against_the_proposed_side():
    cfg = config_from_dict({"gate_stack": STACK + ["location"]})
    series = synthetic(20_000, seed=312)
    v = collect_verdicts(series, cfg, STACK + ["location"])
    confirmed = v.passed["location"]
    # A confirmation gate can only ever agree with the side it was given.
    assert np.all(v.direction["location"][confirmed] == v.proposed[confirmed])
    # With no side proposed it cannot pass.
    assert not np.any(confirmed & (v.proposed == FLAT))


# --------------------------------------------------------------------------- #
# pairwise overlap
# --------------------------------------------------------------------------- #

def test_pair_overlap_covers_every_pair(rig):
    _, _, _, v = rig
    rows = verdict_overlap(v)
    assert len(rows) == len(STACK) * (len(STACK) - 1) // 2
    for r in rows:
        assert -1.0 <= r.phi <= 1.0
        assert 0.0 <= r.jaccard <= 1.0


def test_a_gate_compared_against_itself_is_a_perfect_duplicate(rig):
    """Sanity check on the metric: identical inputs must read as identical."""
    _, _, _, v = rig
    v.names = v.names + ["trend_clone"]
    v.passed["trend_clone"] = v.passed["trend_alignment"].copy()
    v.direction["trend_clone"] = v.direction["trend_alignment"].copy()
    pair = next(r for r in verdict_overlap(v)
                if {r.a, r.b} == {"trend_alignment", "trend_clone"})
    assert pair.phi == pytest.approx(1.0)
    assert pair.jaccard == pytest.approx(1.0)
    assert pair.direction_agreement == pytest.approx(1.0)
    assert pair.verdict == "near-duplicate"
    v.names.remove("trend_clone")
    del v.passed["trend_clone"], v.direction["trend_clone"]


def test_independent_verdicts_read_as_independent():
    from btc5m.redundancy import Verdicts

    rng = np.random.default_rng(0)
    n = 5000
    a = rng.random(n) < 0.4
    b = rng.random(n) < 0.4
    # Independent sides too: two gates that always vote the same way are
    # duplicates on direction even when they fire on unrelated bars.
    dir_a = np.where(a, np.where(rng.random(n) < 0.5, UP, DOWN), FLAT)
    dir_b = np.where(b, np.where(rng.random(n) < 0.5, UP, DOWN), FLAT)
    v = Verdicts(names=["a", "b"], passed={"a": a, "b": b},
                 direction={"a": dir_a.astype(np.int8), "b": dir_b.astype(np.int8)},
                 proposed=np.zeros(n, np.int8), outcome=np.ones(n, int),
                 start=0, bars=n)
    pair = verdict_overlap(v)[0]
    assert abs(pair.phi) < 0.05
    assert pair.lift == pytest.approx(1.0, abs=0.1)
    assert pair.direction_agreement == pytest.approx(0.5, abs=0.05)
    assert pair.verdict == "largely independent"


def test_matching_directions_alone_make_a_near_duplicate():
    """Two gates can fire on unrelated bars and still be one directional opinion."""
    from btc5m.redundancy import Verdicts

    rng = np.random.default_rng(1)
    n = 5000
    a = rng.random(n) < 0.4
    b = rng.random(n) < 0.4
    side = np.where(rng.random(n) < 0.5, UP, DOWN)
    v = Verdicts(names=["a", "b"], passed={"a": a, "b": b},
                 direction={"a": np.where(a, side, FLAT).astype(np.int8),
                            "b": np.where(b, side, FLAT).astype(np.int8)},
                 proposed=np.zeros(n, np.int8), outcome=np.ones(n, int),
                 start=0, bars=n)
    pair = verdict_overlap(v)[0]
    assert abs(pair.phi) < 0.05            # independent on when they fire
    assert pair.direction_agreement == pytest.approx(1.0)
    assert pair.verdict == "near-duplicate"


def test_lift_of_one_means_no_information():
    p = PairOverlap("a", "b", phi=0.0, jaccard=0.2, p_b=0.4, p_b_given_a=0.4,
                    direction_agreement=0.5, both_pass=500)
    assert p.lift == pytest.approx(1.0)
    assert p.verdict == "largely independent"


def test_a_small_joint_sample_is_not_judged():
    p = PairOverlap("a", "b", phi=0.95, jaccard=0.9, p_b=0.5, p_b_given_a=0.99,
                    direction_agreement=1.0, both_pass=10)
    assert p.verdict == "too few joint passes to judge"


# --------------------------------------------------------------------------- #
# marginal value -- the measurement that decides
# --------------------------------------------------------------------------- #

def test_marginal_value_scores_only_gates_with_a_side(rig):
    _, _, _, v = rig
    rows = marginal_value(v, STACK)
    scored = {r.gate for r in rows}
    assert scored == {"trend_alignment", "persistence", "participation"}


def test_marginal_value_populates_both_buckets(rig):
    """The bug this guards: taking the side from the full stack empties one side."""
    _, _, _, v = rig
    for r in marginal_value(v, STACK):
        assert r.with_gate > 0, f"{r.gate}: no bars where it confirmed"
        assert r.without_gate > 0, f"{r.gate}: no bars where it did not"
        assert r.with_gate + r.without_gate == r.rest_passes


def test_marginal_value_arithmetic():
    m = MarginalValue(gate="g", rest_passes=1000, with_gate=400,
                      without_gate=600, hit_with=0.60, hit_without=0.50)
    assert m.separation == pytest.approx(0.10)
    assert m.z is not None and m.z > 2.0
    assert m.verdict == "adds information"
    assert m.signals_filtered == pytest.approx(0.60)


def test_no_separation_is_reported_as_redundant():
    m = MarginalValue(gate="g", rest_passes=2000, with_gate=1000,
                      without_gate=1000, hit_with=0.55, hit_without=0.55)
    assert m.separation == pytest.approx(0.0)
    assert m.verdict == "no information beyond the other gates"


def test_an_inverted_gate_is_called_out():
    m = MarginalValue(gate="g", rest_passes=4000, with_gate=2000,
                      without_gate=2000, hit_with=0.45, hit_without=0.60)
    assert m.verdict == "inverted: the failing side wins more"


def test_a_tiny_bucket_is_not_judged():
    m = MarginalValue(gate="g", rest_passes=60, with_gate=10, without_gate=50,
                      hit_with=1.0, hit_without=0.0)
    assert m.verdict == "too few bars to judge"


# --------------------------------------------------------------------------- #
# leave-one-out
# --------------------------------------------------------------------------- #

def test_leave_one_out_reports_the_full_stack_plus_each_removal(rig):
    cfg, series, _, _ = rig
    rows = leave_one_out(series, cfg)
    assert rows[0].removed == "(none)"
    removed = {r.removed for r in rows[1:]}
    assert removed <= set(cfg.gate_stack)


def test_leave_one_out_on_a_three_gate_stack_has_nothing_to_remove():
    """The validator floors a stack at three, so every removal is skipped."""
    cfg = config_from_dict()
    assert len(cfg.gate_stack) == 3
    rows = leave_one_out(synthetic(6000, seed=314), cfg)
    assert len(rows) == 1
    assert rows[0].removed == "(none)"


def test_leave_one_out_removes_gates_when_the_stack_can_spare_them():
    cfg = config_from_dict({"gate_stack": ["data_integrity", "volatility_regime",
                                           "trend_alignment", "persistence"]})
    rows = leave_one_out(synthetic(6000, seed=315), cfg)
    assert len(rows) >= 2
    assert {r.removed for r in rows[1:]} <= set(cfg.gate_stack)


def test_removing_a_gate_cannot_tighten_the_gate_filter(rig):
    """Fewer gates means at least as many bars pass the gates themselves."""
    _, _, _, v = rig
    full = np.ones(v.bars, dtype=bool)
    for name in STACK:
        full &= v.passed[name]
    for name in STACK:
        without = np.ones(v.bars, dtype=bool)
        for other in STACK:
            if other != name:
                without &= v.passed[other]
        assert without.sum() >= full.sum()


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #

def test_priced_edge_is_flagged_as_never_binding_at_defaults(rig):
    cfg, series, _, _ = rig
    rows = {r.name: r for r in condition_bindings(series, cfg)}
    edge = rows["priced_edge"]
    assert not edge.can_ever_bind
    assert "never binds" in edge.note
    assert edge.blocks == 0


def test_priced_edge_binds_once_the_payout_makes_it_the_tighter_test():
    cfg = config_from_dict({"betting": {"net_payout": 0.80}})
    rows = {r.name: r for r in condition_bindings(synthetic(8000, seed=313), cfg)}
    assert rows["priced_edge"].can_ever_bind
    assert "binds for conviction" in rows["priced_edge"].note


def test_timing_is_reported_as_untestable_on_history(rig):
    cfg, series, _, _ = rig
    rows = {r.name: r for r in condition_bindings(series, cfg)}
    assert "not testable on history" in rows["timing"].note


def test_gate_agreement_is_the_condition_that_does_the_work(rig):
    cfg, series, _, _ = rig
    rows = {r.name: r for r in condition_bindings(series, cfg)}
    assert rows["gate_agreement"].blocks > 0
    assert rows["gate_agreement"].can_ever_bind


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def test_full_report_covers_all_five_sections(rig):
    cfg, series, _, _ = rig
    text = full_report(series, cfg)
    for heading in ("1. STRUCTURAL OVERLAP", "2. VERDICT OVERLAP",
                    "3. MARGINAL VALUE", "4. LEAVE-ONE-OUT",
                    "5. BETTING CONDITIONS"):
        assert heading in text
    for name in cfg.gate_stack:
        assert name in text


# --------------------------------------------------------------------------- #
# the single effective threshold, and the risk-side overlaps
# --------------------------------------------------------------------------- #

def test_the_effective_floor_is_the_higher_of_the_two_conditions():
    generous = config_from_dict({"betting": {"net_payout": 0.95}})
    stingy = config_from_dict({"betting": {"net_payout": 0.80}})
    assert generous.effective_conviction_floor() == pytest.approx(
        generous.betting.min_conviction)
    assert stingy.effective_conviction_floor() == pytest.approx(
        stingy.implied_conviction_floor())
    assert stingy.effective_conviction_floor() > generous.effective_conviction_floor()


def test_the_report_states_the_single_effective_threshold(rig):
    cfg, series, _, _ = rig
    text = full_report(series, cfg)
    assert "two thresholds on one scalar" in text
    assert f"{cfg.effective_conviction_floor():.3f}" in text


def test_the_stake_cap_binds_at_every_allowed_conviction():
    """Risk condition 1's two halves overlap: at default settings only the cap sizes."""
    from btc5m.risk import RiskManager
    from btc5m.signal import SignalEngine

    cfg = config_from_dict()
    engine = SignalEngine(cfg)
    rm = RiskManager(cfg.risk, engine.break_even, engine.odds)
    stakes = []
    for conviction in (0.60, 0.70, 0.80, 0.90, 1.00):
        decision = rm.assess(bar_index=1, ts=1_699_920_000,
                             p_model=engine._probability(conviction))
        stakes.append(decision.stake)
        assert "set by cap" in decision.conditions[0].detail
    # Same stake at every conviction: kelly_fraction is not sizing anything.
    assert len(set(stakes)) == 1


def test_kelly_sizes_the_bet_once_the_cap_is_lifted():
    from btc5m.risk import RiskManager
    from btc5m.signal import SignalEngine

    cfg = config_from_dict({"risk": {"max_stake_pct": 0.5}})
    engine = SignalEngine(cfg)
    rm = RiskManager(cfg.risk, engine.break_even, engine.odds)
    low = rm.assess(bar_index=1, ts=1_699_920_000,
                    p_model=engine._probability(0.60))
    high = rm.assess(bar_index=1, ts=1_699_920_000,
                     p_model=engine._probability(1.00))
    assert "set by Kelly" in low.conditions[0].detail
    assert high.stake > low.stake


def test_the_halt_flag_belongs_to_max_drawdown_alone():
    """Both conditions used to fail together, double-counting every post-halt bar."""
    from btc5m.risk import OpenBet, RiskManager

    cfg = config_from_dict({"risk": {"max_drawdown_pct": 0.10,
                                     "daily_loss_limit_pct": 1.0,
                                     "max_consecutive_losses": 99,
                                     "max_bets_per_hour": 99}})
    rm = RiskManager(cfg.risk, cfg.break_even_probability(), cfg.payoff_odds())
    for k in range(6):
        bet = OpenBet(10 + k, 1_699_920_000 + k * 86_400, UP, 250.0, 60_000.0,
                      1_699_920_000 + k * 86_400 + 300, 11 + k, 0.59, 0.7)
        rm.open(bet)
        rm.settle(bet, 59_900.0, "loss")
    assert rm.halted
    decision = rm.assess(bar_index=999, ts=1_699_920_000 + 40 * 86_400,
                         p_model=0.60)
    assert decision.blocked_by == ("max_drawdown",)


# --------------------------------------------------------------------------- #
# 3b. how two directional gates combine
# --------------------------------------------------------------------------- #

def _two_gate_verdicts():
    """Eight graded bars.  a fires on 0-4, b on 2-7; they agree on 2 and 3,
    disagree on 4; outcomes chosen so every cell has a known hit rate."""
    from btc5m.redundancy import Verdicts
    n = 8
    a_pass = np.array([1, 1, 1, 1, 1, 0, 0, 0], dtype=bool)
    b_pass = np.array([0, 0, 1, 1, 1, 1, 1, 1], dtype=bool)
    a_dir = np.array([1, 1, 1, -1, 1, 0, 0, 0], dtype=np.int8)
    b_dir = np.array([0, 0, 1, -1, -1, 1, -1, 1], dtype=np.int8)
    outcome = np.array([1, -1, 1, -1, -1, 1, 1, 1], dtype=int)
    return Verdicts(names=["a", "b"], passed={"a": a_pass, "b": b_pass},
                    direction={"a": a_dir, "b": b_dir},
                    proposed=np.zeros(n, dtype=np.int8), outcome=outcome,
                    start=0, bars=n, break_even=0.52)


def test_combination_cells_partition_the_bars():
    from btc5m.redundancy import combine_pair
    rows = {r.label: r for r in combine_pair(_two_gate_verdicts(), "a", "b")}
    a_only = rows["a alone"]                  # bars 0, 1: right, wrong
    b_only = rows["b alone"]                  # bars 5, 6, 7: right, wrong, right
    agree = rows["both fire, agree"]          # bars 2, 3: both right
    disagree = rows["both fire, disagree"]    # bar 4
    assert (a_only.signals, a_only.wins, a_only.losses) == (2, 1, 1)
    assert (b_only.signals, b_only.wins, b_only.losses) == (3, 2, 1)
    assert (agree.signals, agree.wins, agree.losses) == (2, 2, 0)
    assert disagree.signals == 1 and disagree.wins == 0 and disagree.losses == 0
    assert rows["AND: both must agree (unanimous)"].signals == agree.signals
    either = rows["OR: either, no dissent (weighted)"]
    assert either.signals == a_only.signals + b_only.signals + agree.signals
    assert either.wins == 1 + 2 + 2 and either.losses == 1 + 1 + 0
    assert either.break_even == 0.52


def test_combination_renders_and_names_the_wirings():
    from btc5m.redundancy import combine_pair, render_combination
    v = _two_gate_verdicts()
    text = render_combination(combine_pair(v, "a", "b"), "a", "b")
    assert "HOW a AND b COMBINE" in text
    for needle in ("a alone", "b alone", "both fire, agree", "both fire, disagree",
                   "AND: both must agree", "OR: either, no dissent",
                   "no side to grade", "unanimous", "weighted"):
        assert needle in text, needle


def test_full_report_adds_the_section_only_for_two_directional_gates(series):
    from btc5m.redundancy import full_report
    two = config_from_dict({"gate_stack": ["data_integrity", "mean_reversion",
                                           "taker_flow", "session"],
                            "betting": {"min_directional_gates": 1}})
    one = config_from_dict({"gate_stack": ["data_integrity", "mean_reversion",
                                           "session"],
                            "betting": {"min_directional_gates": 1}})
    assert "3b. HOW mean_reversion AND taker_flow COMBINE" in full_report(series, two)
    assert "3b." not in full_report(series, one)
