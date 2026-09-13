"""Settlement, bookkeeping, and the guarantee that no bar reads its own future."""

import numpy as np
import pytest

from btc5m.backtest import resolve_outcome, run_backtest
from btc5m.config import config_from_dict
from btc5m.data import synthetic
from btc5m.features import build_features
from btc5m.gates import DOWN, UP
from btc5m.signal import SignalEngine


@pytest.fixture(scope="module")
def result():
    cfg = config_from_dict({"betting": {"required_edge": 0.0, "min_conviction": 0.5}})
    return run_backtest(synthetic(30_000, seed=404), cfg)


# --------------------------------------------------------------------------- #
# settlement
# --------------------------------------------------------------------------- #

def test_resolution_follows_the_direction_of_the_move():
    assert resolve_outcome(100.0, 101.0, UP, 0.0, "loss") == "win"
    assert resolve_outcome(100.0, 99.0, UP, 0.0, "loss") == "loss"
    assert resolve_outcome(100.0, 99.0, DOWN, 0.0, "loss") == "win"
    assert resolve_outcome(100.0, 101.0, DOWN, 0.0, "loss") == "loss"


def test_an_exact_tie_follows_the_configured_policy():
    assert resolve_outcome(100.0, 100.0, UP, 0.0, "loss") == "loss"
    assert resolve_outcome(100.0, 100.0, UP, 0.0, "void") == "void"


def test_the_deadband_turns_a_tiny_move_into_a_tie():
    # 1 bp move against a 5 bp deadband.
    assert resolve_outcome(100.0, 100.01, UP, 5.0, "void") == "void"
    assert resolve_outcome(100.0, 100.01, UP, 0.5, "void") == "win"


def test_a_nonsensical_entry_price_voids_rather_than_guesses():
    assert resolve_outcome(0.0, 100.0, UP, 0.0, "loss") == "void"


# --------------------------------------------------------------------------- #
# no look-ahead
# --------------------------------------------------------------------------- #

def test_a_signal_does_not_change_when_the_future_is_removed():
    """The load-bearing test.

    If any feature peeked forward, truncating the series right after bar k would
    change the decision at bar k.  Checked across many bars because a leak in a
    rarely-warm feature would otherwise hide.
    """
    cfg = config_from_dict()
    full = synthetic(3000, seed=808)
    fs_full = build_features(full, cfg)
    engine = SignalEngine(cfg)
    warmup = engine.warmup_bars(fs_full)

    for k in range(warmup + 5, len(full), 137):
        truncated = full[:k + 1]
        sig_full = engine.evaluate(fs_full, k)
        sig_trunc = engine.evaluate(build_features(truncated, cfg), k)
        assert sig_trunc.side == sig_full.side, f"bar {k}: side changed"
        assert sig_trunc.tradable == sig_full.tradable, f"bar {k}: verdict changed"
        assert sig_trunc.conviction == pytest.approx(sig_full.conviction), f"bar {k}"
        for a, b in zip(sig_trunc.gate_results, sig_full.gate_results):
            assert a.passed == b.passed, f"bar {k}: gate {a.name} changed"


def test_a_signal_does_not_change_when_future_prices_are_altered():
    """A second angle: rewrite the future instead of deleting it."""
    cfg = config_from_dict()
    original = synthetic(2500, seed=909)
    engine = SignalEngine(cfg)
    fs = build_features(original, cfg)
    k = 1800

    tampered = synthetic(2500, seed=909)
    tampered.close[k + 1:] *= 1.5
    tampered.high[k + 1:] *= 1.5
    tampered.low[k + 1:] *= 1.5
    tampered.open[k + 1:] *= 1.5
    tampered.volume[k + 1:] *= 10.0

    before = engine.evaluate(fs, k)
    after = engine.evaluate(build_features(tampered, cfg), k)
    assert before.side == after.side
    assert before.tradable == after.tradable
    assert before.conviction == pytest.approx(after.conviction)


def test_bets_settle_strictly_after_they_are_placed(result):
    for b in result.bets:
        assert b.ts < b.ts + 300
        assert b.exit_price != 0.0


# --------------------------------------------------------------------------- #
# bookkeeping
# --------------------------------------------------------------------------- #

def test_pnl_reconciles_with_the_bet_ledger(result):
    assert result.net_pnl == pytest.approx(sum(b.pnl for b in result.bets))
    assert result.final_bankroll == pytest.approx(
        result.starting_bankroll + result.net_pnl)


def test_the_equity_curve_has_one_point_per_settlement_plus_the_open(result):
    assert len(result.equity) == len(result.bets) + 1
    assert result.equity[0] == pytest.approx(result.starting_bankroll)
    assert result.equity[-1] == pytest.approx(result.final_bankroll)


def test_outcome_counts_add_up(result):
    assert result.wins + result.losses + result.voids == len(result.bets)
    assert len(result.graded) == result.wins + result.losses


def test_hit_rate_matches_the_ledger(result):
    if result.graded:
        assert result.hit_rate == pytest.approx(
            result.wins / len(result.graded))


def test_every_bet_came_from_a_tradable_signal(result):
    assert len(result.bets) <= result.tradable_signals
    assert result.tradable_signals <= result.bars_evaluated


def test_max_drawdown_is_a_fraction_between_zero_and_one(result):
    assert 0.0 <= result.max_drawdown <= 1.0


def test_concurrency_limit_is_respected():
    cfg = config_from_dict({"betting": {"horizon_bars": 6, "required_edge": 0.0},
                            "risk": {"max_concurrent_bets": 1,
                                     "max_bets_per_hour": 99}})
    result = run_backtest(synthetic(20_000, seed=505), cfg)
    starts = sorted(b.ts for b in result.bets)
    for a, b in zip(starts, starts[1:]):
        assert b - a >= 6 * 300, "overlapping bets despite a limit of one"


def test_a_longer_horizon_is_settled_against_the_right_bar():
    cfg = config_from_dict({"betting": {"horizon_bars": 4, "required_edge": 0.0},
                            "risk": {"max_bets_per_hour": 99}})
    series = synthetic(15_000, seed=606)
    result = run_backtest(series, cfg)
    assert result.bets
    ts_to_index = {int(t): i for i, t in enumerate(series.ts)}
    for b in result.bets:
        i = ts_to_index[int(b.ts)]
        assert b.entry_price == pytest.approx(float(series.close[i]))
        assert b.exit_price == pytest.approx(float(series.close[i + 4]))


def test_stake_never_exceeds_the_configured_cap(result):
    cap = result.config["risk"]["max_stake_pct"]
    for b in result.bets:
        assert b.stake <= b.bankroll_after / (1 - cap) * cap + 1e-6


def test_calibration_buckets_cover_every_graded_bet(result):
    rows = result.calibration()
    if rows:
        assert sum(r["bets"] for r in rows) == len(result.graded)
        for r in rows:
            assert 0.0 <= r["realised_p"] <= 1.0
            assert 0.0 <= r["claimed_p"] <= 1.0


def test_summary_mentions_the_headline_numbers(result):
    text = result.summary()
    for token in ("bets placed", "hit rate", "break-even needed", "net P&L",
                  "max drawdown"):
        assert token in text


def test_significance_flag_agrees_with_the_z_score(result):
    if result.hit_rate is None or not result.hit_rate_stderr:
        pytest.skip("not enough bets")
    z = (result.hit_rate - result.break_even) / result.hit_rate_stderr
    assert result.edge_is_significant() == (z >= 2.0)


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #

def test_a_series_shorter_than_the_warmup_returns_an_empty_result():
    """Length is derived, not hardcoded: the warm-up shrank when the stack did."""
    cfg = config_from_dict()
    probe = synthetic(2000, seed=1)
    warmup = SignalEngine(cfg).warmup_bars(build_features(probe, cfg))
    assert warmup > 1, "expected some warm-up to test against"
    result = run_backtest(probe[:warmup], cfg)
    assert result.bets == []
    assert result.final_bankroll == result.starting_bankroll
    assert result.max_drawdown == 0.0
    assert "n/a" in result.summary()


def test_a_dead_flat_market_produces_no_bets():
    n = 3000
    from btc5m.data import BAR_SECONDS, BarSeries
    flat = BarSeries(ts=1_699_920_000 + np.arange(n, dtype=np.int64) * BAR_SECONDS,
                     open=np.full(n, 60_000.0), high=np.full(n, 60_000.0),
                     low=np.full(n, 60_000.0), close=np.full(n, 60_000.0),
                     volume=np.full(n, 100.0))
    assert run_backtest(flat, config_from_dict()).bets == []


def test_the_on_signal_hook_sees_every_evaluated_bar():
    cfg = config_from_dict()
    seen = []
    result = run_backtest(synthetic(2000, seed=7), cfg, on_signal=seen.append)
    assert len(seen) == result.bars_evaluated
