"""The three risk conditions: stake sizing, loss limits, strategy health."""

import pytest

from btc5m.config import config_from_dict
from btc5m.gates import UP
from btc5m.risk import OpenBet, RiskManager

DAY = 86_400
# Exactly 00:00:00 UTC. The daily loss budget resets on the UTC day boundary,
# so a fixture anchored mid-evening would silently straddle midnight and hand
# the strategy a fresh budget partway through the test.
BASE_TS = 1_699_920_000
BAR = 300


def manager(**risk_overrides):
    cfg = config_from_dict({"risk": risk_overrides} if risk_overrides else {})
    return RiskManager(cfg.risk, cfg.break_even_probability(), cfg.payoff_odds())


def bet(rm, *, bar=10, ts=BASE_TS, side=UP, stake=100.0, price=60_000.0, p=0.59):
    b = OpenBet(bar_index=bar, ts=ts, side=side, stake=stake, entry_price=price,
                expiry_ts=ts + BAR, expiry_bar=bar + 1, p_model=p, conviction=0.7)
    rm.open(b)
    return b


def play(rm, outcome, *, bar=10, ts=BASE_TS, stake=100.0):
    b = bet(rm, bar=bar, ts=ts, stake=stake)
    exit_price = 60_100.0 if outcome == "win" else 59_900.0
    return rm.settle(b, exit_price, outcome)


def condition(decision, label):
    found = [c for c in decision.conditions if c.label == label]
    assert found, f"no condition {label!r}; has {[c.label for c in decision.conditions]}"
    return found[0]


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #

def test_rejects_impossible_odds_and_probabilities():
    cfg = config_from_dict()
    with pytest.raises(ValueError):
        RiskManager(cfg.risk, 0.0, 0.9)
    with pytest.raises(ValueError):
        RiskManager(cfg.risk, 1.0, 0.9)
    with pytest.raises(ValueError):
        RiskManager(cfg.risk, 0.5, 0.0)


# --------------------------------------------------------------------------- #
# condition 1: stake sizing
# --------------------------------------------------------------------------- #

def test_kelly_grows_with_the_edge():
    rm = manager(max_stake_pct=1.0)
    stakes = [rm.assess(bar_index=1, ts=BASE_TS, p_model=p).stake
              for p in (0.54, 0.58, 0.65, 0.75)]
    assert stakes == sorted(stakes)
    assert all(s > 0 for s in stakes)


def test_no_edge_means_no_bet():
    rm = manager()
    decision = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.50)
    assert not decision.approved
    assert "stake_sizing" in decision.blocked_by
    assert "no edge" in condition(decision, "stake_sizing").detail
    assert decision.stake == 0.0


def test_stake_cap_binds_before_kelly_does():
    rm = manager(max_stake_pct=0.02, kelly_fraction=1.0)
    decision = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.75)
    assert decision.approved
    assert decision.stake == pytest.approx(rm.bankroll * 0.02)
    assert decision.stake_pct == pytest.approx(0.02)


def test_kelly_fraction_scales_the_stake():
    quarter = manager(max_stake_pct=1.0, kelly_fraction=0.25)
    half = manager(max_stake_pct=1.0, kelly_fraction=0.5)
    a = quarter.assess(bar_index=1, ts=BASE_TS, p_model=0.60).stake
    b = half.assess(bar_index=1, ts=BASE_TS, p_model=0.60).stake
    assert b == pytest.approx(2 * a, rel=0.02)


def test_stake_below_the_minimum_is_refused():
    rm = manager(max_stake_pct=0.02, min_stake=10_000.0)
    decision = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60)
    assert not decision.approved
    assert "minimum stake" in condition(decision, "stake_sizing").detail


def test_stake_is_rounded_to_the_configured_increment():
    rm = manager(max_stake_pct=1.0, stake_rounding=25.0)
    stake = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.62).stake
    assert stake % 25.0 == pytest.approx(0.0)


def test_stake_shrinks_as_the_bankroll_does():
    rm = manager()
    first = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60).stake
    for k in range(5):
        play(rm, "loss", bar=100 + k, ts=BASE_TS + k * DAY, stake=500.0)
    assert rm.bankroll < 10_000.0
    later = rm.assess(bar_index=999, ts=BASE_TS + 9 * DAY, p_model=0.60).stake
    assert later < first


# --------------------------------------------------------------------------- #
# condition 2: loss limits and exposure
# --------------------------------------------------------------------------- #

def test_daily_loss_limit_stops_betting_for_the_day():
    rm = manager(daily_loss_limit_pct=0.05, max_consecutive_losses=99,
                 max_bets_per_hour=99)
    for k in range(6):
        play(rm, "loss", bar=10 + k, ts=BASE_TS + k * 3600, stake=100.0)
    decision = rm.assess(bar_index=100, ts=BASE_TS + 7 * 3600, p_model=0.60)
    assert not decision.approved
    assert "daily_loss_limit" in decision.blocked_by


def test_a_new_utc_day_resets_the_daily_budget():
    rm = manager(daily_loss_limit_pct=0.05, max_consecutive_losses=99,
                 max_bets_per_hour=99)
    for k in range(6):
        play(rm, "loss", bar=10 + k, ts=BASE_TS + k * 3600, stake=100.0)
    assert "daily_loss_limit" in rm.assess(bar_index=100, ts=BASE_TS + 7 * 3600,
                                           p_model=0.60).blocked_by
    tomorrow = rm.assess(bar_index=500, ts=BASE_TS + 2 * DAY, p_model=0.60)
    assert "daily_loss_limit" not in tomorrow.blocked_by


def test_max_drawdown_halts_permanently():
    rm = manager(max_drawdown_pct=0.10, daily_loss_limit_pct=1.0,
                 max_consecutive_losses=99, max_bets_per_hour=99)
    for k in range(6):
        play(rm, "loss", bar=10 + k, ts=BASE_TS + k * DAY, stake=250.0)
    assert rm.halted
    assert "drawdown" in rm.halt_reason
    far_future = rm.assess(bar_index=9_999, ts=BASE_TS + 99 * DAY, p_model=0.70)
    assert not far_future.approved
    assert "max_drawdown" in far_future.blocked_by


def test_consecutive_losses_trigger_a_cooldown():
    rm = manager(max_consecutive_losses=3, cooldown_bars=6,
                 daily_loss_limit_pct=1.0, max_bets_per_hour=99)
    for k in range(3):
        play(rm, "loss", bar=10 + k, ts=BASE_TS + k * 3600, stake=50.0)
    assert rm.consecutive_losses == 3
    assert "loss_streak_cooldown" in rm.assess(
        bar_index=14, ts=BASE_TS + 4 * 3600, p_model=0.60).blocked_by
    assert "loss_streak_cooldown" not in rm.assess(
        bar_index=25, ts=BASE_TS + 5 * 3600, p_model=0.60).blocked_by


def test_a_win_clears_the_loss_streak():
    rm = manager(max_consecutive_losses=3, daily_loss_limit_pct=1.0,
                 max_bets_per_hour=99)
    play(rm, "loss", bar=10, ts=BASE_TS)
    play(rm, "loss", bar=11, ts=BASE_TS + 3600)
    assert rm.consecutive_losses == 2
    play(rm, "win", bar=12, ts=BASE_TS + 7200)
    assert rm.consecutive_losses == 0


def test_bets_per_hour_is_capped_and_the_window_rolls():
    rm = manager(max_bets_per_hour=2, max_concurrent_bets=5,
                 daily_loss_limit_pct=1.0)
    bet(rm, bar=1, ts=BASE_TS)
    bet(rm, bar=2, ts=BASE_TS + 60)
    assert "bets_per_hour" in rm.assess(bar_index=3, ts=BASE_TS + 120,
                                       p_model=0.60).blocked_by
    later = rm.assess(bar_index=4, ts=BASE_TS + 4000, p_model=0.60)
    assert "bets_per_hour" not in later.blocked_by


def test_concurrent_bets_are_capped():
    rm = manager(max_concurrent_bets=1, max_bets_per_hour=99)
    bet(rm, bar=1, ts=BASE_TS)
    assert "concurrent_bets" in rm.assess(bar_index=2, ts=BASE_TS + 300,
                                          p_model=0.60).blocked_by


# --------------------------------------------------------------------------- #
# condition 3: strategy health
# --------------------------------------------------------------------------- #

def test_health_is_unjudged_until_there_are_enough_bets():
    rm = manager(health_min_samples=15)
    assert rm.rolling_hit_rate() is None
    decision = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60)
    assert decision.approved
    assert "need 15" in condition(decision, "strategy_health").detail


def test_a_losing_run_derates_the_stake_rather_than_stopping():
    rm = manager(health_min_samples=10, health_window=20, hit_rate_buffer=0.04,
                 unhealthy_stake_derate=0.5, daily_loss_limit_pct=1.0,
                 max_drawdown_pct=1.0, max_consecutive_losses=99,
                 max_bets_per_hour=99)
    baseline = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60).stake
    for k in range(12):
        play(rm, "loss" if k % 4 else "win", bar=10 + k, ts=BASE_TS + k * DAY,
             stake=10.0)
    decision = rm.assess(bar_index=500, ts=BASE_TS + 20 * DAY, p_model=0.60)
    assert decision.approved
    assert decision.derated
    assert decision.stake < baseline
    assert "derating" in condition(decision, "strategy_health").detail


def test_halt_when_unhealthy_stops_betting_entirely():
    rm = manager(health_min_samples=10, health_window=20,
                 halt_when_unhealthy=True, daily_loss_limit_pct=1.0,
                 max_drawdown_pct=1.0, max_consecutive_losses=99,
                 max_bets_per_hour=99)
    for k in range(12):
        play(rm, "loss" if k % 4 else "win", bar=10 + k, ts=BASE_TS + k * DAY,
             stake=10.0)
    decision = rm.assess(bar_index=500, ts=BASE_TS + 20 * DAY, p_model=0.60)
    assert not decision.approved
    assert "strategy_health" in decision.blocked_by


def test_a_winning_run_stays_healthy_and_undertated():
    rm = manager(health_min_samples=10, health_window=20, max_bets_per_hour=99,
                 max_concurrent_bets=99)
    for k in range(14):
        play(rm, "win", bar=10 + k, ts=BASE_TS + k * DAY, stake=10.0)
    decision = rm.assess(bar_index=500, ts=BASE_TS + 20 * DAY, p_model=0.60)
    assert decision.approved
    assert not decision.derated
    assert rm.rolling_hit_rate() == pytest.approx(1.0)


def test_a_volatility_shock_stands_the_strategy_down():
    rm = manager(atr_shock_rank=0.985)
    calm = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60, atr_rank=0.5)
    shock = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.60, atr_rank=0.99)
    assert calm.approved
    assert not shock.approved
    assert "strategy_health" in shock.blocked_by
    assert "volatility shock" in condition(shock, "strategy_health").detail


# --------------------------------------------------------------------------- #
# settlement bookkeeping
# --------------------------------------------------------------------------- #

def test_a_win_pays_the_odds_and_a_loss_costs_the_stake():
    rm = manager()
    won = play(rm, "win", stake=100.0)
    assert won.pnl == pytest.approx(90.0)
    assert rm.bankroll == pytest.approx(10_090.0)
    lost = play(rm, "loss", bar=20, ts=BASE_TS + 3600, stake=100.0)
    assert lost.pnl == pytest.approx(-100.0)
    assert rm.bankroll == pytest.approx(9_990.0)


def test_a_void_returns_the_stake_and_is_not_graded():
    rm = manager()
    b = bet(rm, stake=100.0)
    record = rm.settle(b, 60_000.0, "void")
    assert record.pnl == pytest.approx(0.0)
    assert rm.bankroll == pytest.approx(10_000.0)
    assert rm.consecutive_losses == 0
    assert rm.rolling_hit_rate() is None


def test_unknown_outcome_and_zero_stake_are_rejected():
    rm = manager()
    b = bet(rm)
    with pytest.raises(ValueError):
        rm.settle(b, 60_000.0, "banana")
    with pytest.raises(ValueError):
        rm.open(OpenBet(1, BASE_TS, UP, 0.0, 60_000.0, BASE_TS + 300, 2, 0.6, 0.7))


def test_peak_bankroll_and_drawdown_track_the_equity_curve():
    rm = manager(max_drawdown_pct=1.0, daily_loss_limit_pct=1.0,
                 max_consecutive_losses=99, max_bets_per_hour=99)
    play(rm, "win", bar=1, ts=BASE_TS, stake=1_000.0)
    assert rm.peak_bankroll == pytest.approx(10_900.0)
    play(rm, "loss", bar=2, ts=BASE_TS + DAY, stake=1_000.0)
    assert rm.drawdown == pytest.approx(1_000.0 / 10_900.0)


def test_exposure_reflects_open_bets_only():
    rm = manager(max_concurrent_bets=9, max_bets_per_hour=99)
    a = bet(rm, bar=1, ts=BASE_TS, stake=100.0)
    bet(rm, bar=2, ts=BASE_TS + 300, stake=250.0)
    assert rm.exposure == pytest.approx(350.0)
    rm.settle(a, 60_100.0, "win")
    assert rm.exposure == pytest.approx(250.0)


def test_report_names_the_blocking_condition():
    rm = manager()
    text = rm.assess(bar_index=1, ts=BASE_TS, p_model=0.50).report()
    assert "REFUSED" in text
    assert "stake_sizing" in text
