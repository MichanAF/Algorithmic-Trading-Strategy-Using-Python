"""Risk conditions, and the ladder that prices a squeeze before it happens."""

import pytest

from mmhedge.config import config_from_dict
from mmhedge.risk import assess, build_ladder
from mmhedge.sizing import allocate


def condition(report, label):
    found = [c for c in report.checks if c.label == label]
    assert found, f"no condition {label!r}; has {[c.label for c in report.checks]}"
    return found[0]


def test_a_correctly_sized_book_passes(cfg, alloc):
    r = assess(cfg, alloc, hedge_ratio=1.0, funding_apr=0.20)
    assert r.ok, r.blocked_by


def test_ladder_rungs_are_ordered_by_severity(cfg, alloc):
    ladder = assess(cfg, alloc, hedge_ratio=1.0, entry_price=100_000.0).ladder
    moves = [r.adverse_move for r in ladder.rungs]
    assert moves == sorted(moves)
    assert {r.label for r in ladder.rungs} >= {"watch", "top up", "liq (posted)"}


def test_reserve_extends_the_liquidation_point(cfg, alloc):
    ladder = assess(cfg, alloc, hedge_ratio=1.0).ladder
    assert ladder.liquidation_with_reserve > ladder.liquidation_move
    # And it extends it to exactly the move the book was sized for.
    assert ladder.liquidation_with_reserve == pytest.approx(
        cfg.risk.survive_rally_pct, abs=1e-6)


def test_top_up_equals_the_staged_reserve(cfg, alloc):
    # The allocation and the ladder are two derivations of the same plan; if
    # they disagree, one of them is wrong.
    ladder = assess(cfg, alloc, hedge_ratio=1.0).ladder
    for rung in ladder.rungs:
        if rung.label in ("watch", "top up"):
            assert rung.top_up_usd == pytest.approx(alloc.reserve_usd, rel=1e-3)


def test_equity_falls_as_price_rises(cfg, alloc):
    ladder = assess(cfg, alloc, hedge_ratio=1.0).ladder
    equities = [r.equity_usd for r in ladder.rungs]
    assert equities == sorted(equities, reverse=True)


def test_ladder_prices_render_when_an_entry_is_given(cfg, alloc):
    ladder = assess(cfg, alloc, hedge_ratio=1.0, entry_price=100_000.0).ladder
    watch = next(r for r in ladder.rungs if r.label == "watch")
    assert watch.price == pytest.approx(100_000.0 * (1 + watch.adverse_move))
    assert "100,000" in ladder.report() or "px" in ladder.report()


def test_no_hedge_means_no_ladder(cfg, alloc):
    ladder = build_ladder(cfg, 0.0, 0.0, 0.0)
    assert ladder.rungs == ()
    assert ladder.liquidation_move == float("inf")


def test_buffer_consumption_trips_before_liquidation(cfg, alloc):
    early = assess(cfg, alloc, hedge_ratio=1.0, adverse_move_so_far=0.05)
    late = assess(cfg, alloc, hedge_ratio=1.0, adverse_move_so_far=0.40)
    assert condition(early, "buffer_consumed").passed
    assert not condition(late, "buffer_consumed").passed


def test_unstaged_reserve_is_flagged():
    cfg = config_from_dict({"capital_usd": 25_000.0,
                            "risk": {"reserve_on_arbitrum": False}})
    alloc = allocate(cfg)
    r = assess(cfg, alloc, hedge_ratio=1.0)
    assert not condition(r, "reserve_staged").passed
    assert "will not arrive in time" in condition(r, "reserve_staged").detail


def test_funding_flip_is_reported(cfg, alloc):
    r = assess(cfg, alloc, hedge_ratio=1.0, negative_funding_hours=24)
    assert not condition(r, "funding_regime").passed


def test_proxy_hedges_are_called_what_they_are():
    cfg = config_from_dict({
        "capital_usd": 10_000.0,
        "core": {"weights": {"SOMECOIN": 1.0}, "staked_pct": {}},
        "hedge": {"leverage": 2.0, "hedge_symbol_overrides": {"SOMECOIN": "BTC"}}})
    r = assess(cfg, allocate(cfg), hedge_ratio=1.0)
    c = condition(r, "basis_risk")
    assert not c.passed
    assert "correlation bets" in c.detail


def test_illiquid_core_is_flagged():
    cfg = config_from_dict({
        "capital_usd": 25_000.0,
        "core": {"weights": {"ETH": 1.0}, "staked_pct": {"ETH": 0.9}}})
    r = assess(cfg, allocate(cfg), hedge_ratio=1.0)
    assert not condition(r, "core_liquidity").passed


def test_isolated_margin_is_stated_wherever_a_hedge_exists(cfg, alloc):
    r = assess(cfg, alloc, hedge_ratio=1.0)
    assert any("isolated margin" in n for n in r.notes)
    assert not assess(cfg, alloc, hedge_ratio=0.0).notes


def test_rejects_negative_inputs(cfg):
    with pytest.raises(ValueError):
        build_ladder(cfg, -1.0, 100.0, 0.0)
