"""The live quote watcher. No network and no waiting: the clock, the bar feed
and the venue are all injected, so a whole session runs in milliseconds.

What these tests pin is the part a live run cannot be trusted to reveal: that a
window with no market, a side with no asks and a dead bar feed each produce a
row that says so rather than no row at all, that the bar the engine reads is
recorded with its lag, and that the report's arithmetic is right.
"""

import csv

import pytest

from btc5m.config import config_from_dict
from btc5m.data import synthetic
from btc5m.polymarket import PolymarketError
from btc5m.signal import Signal
from btc5m.venue import DOWN, FLAT, UP
from btc5m.watch import (DEFAULT_OFFSETS, Observation, Watcher, append_row,
                         read_rows, render_report, settle)

START = 1_789_481_100                  # a five-minute boundary
UP_TOKEN, DOWN_TOKEN = "up-token", "down-token"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeMarket:
    """Only the surface the watcher touches."""

    def __init__(self, start=START, outcomes=("Up", "Down"), settled=None):
        self.starts_at = start
        self.ends_at = start + 300
        self.slug = f"btc-updown-5m-{start}"
        self.outcomes = list(outcomes)
        self.token_ids = [UP_TOKEN, DOWN_TOKEN]
        self._settled = settled

    def token_for(self, side):
        want = side.strip().lower()
        for name, token in zip(self.outcomes, self.token_ids):
            if name.strip().lower() == want:
                return token
        return None

    def settled_side(self):
        return self._settled


class FakeBook:
    def __init__(self, bid, ask, depth=100.0):
        self.best_bid, self.best_ask = bid, ask
        self._depth = depth

    def ask_depth(self, up_to=None):
        return 0.0 if self.best_ask is None else self._depth


class FakeClient:
    """A venue that answers from fixtures and counts its calls."""

    def __init__(self, markets=None, books=None, market_error=None, book_error=None):
        self.markets = markets if markets is not None else {START: FakeMarket()}
        self.books = books if books is not None else {
            UP_TOKEN: FakeBook(0.50, 0.51), DOWN_TOKEN: FakeBook(0.48, 0.49)}
        self.market_error, self.book_error = market_error, book_error
        self.market_calls, self.book_calls = [], []

    def market_for_window(self, start, asset="btc", minutes=5):
        self.market_calls.append(start)
        if self.market_error:
            raise PolymarketError(self.market_error)
        return self.markets.get(start)

    def book(self, token_id):
        self.book_calls.append(token_id)
        if self.book_error:
            raise PolymarketError(self.book_error)
        return self.books[token_id]


class Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self, start=START):
        self.t = float(start)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.t += seconds


def bars(n=600, end=START, seed=3):
    """Synthetic bars whose last one closes exactly at ``end``."""
    series = synthetic(n, seed=seed)
    series.ts = series.ts - series.ts[-1] + end
    return series


def config(**over):
    base = {"gate_stack": ["data_integrity", "mean_reversion", "session"],
            "venue": "polymarket-btc-5m",
            "betting": {"min_directional_gates": 1, "fee_bps": 175.0,
                        "tie_policy": "favor_up"},
            "risk": {"starting_bankroll": 1000.0, "max_stake_pct": 0.0075,
                     "min_stake": 5.0}}
    base.update(over)
    return config_from_dict(base)


def watcher(tmp_path, client=None, clock=None, feed=None, **kw):
    clock = clock or Clock()
    series = bars()
    return Watcher(client or FakeClient(), kw.pop("cfg", None) or config(),
                   feed or (lambda: series), out=tmp_path / "q.csv",
                   now=clock.now, sleep=clock.sleep, log=lambda *_: None, **kw), clock


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


def test_one_window_is_read_at_every_offset(tmp_path):
    w, clock = watcher(tmp_path)
    assert w.run(windows=1) == 3
    rows = read_rows(w.out)
    assert [r["offset"] for r in rows] == list(DEFAULT_OFFSETS)
    assert {r["window_start"] for r in rows} == {START}
    # It slept to each offset, not past it.
    assert clock.slept == [5.0, 10.0, 15.0]
    assert [r["observed_ts"] for r in rows] == [START + 5, START + 15, START + 30]


def test_offsets_already_past_are_skipped_and_the_next_window_is_waited_for(tmp_path):
    """Starting 40 seconds into a window: every reading of it has been missed, so
    the watcher waits for the next window rather than reading a stale book."""
    clock = Clock(START + 40)
    w, _ = watcher(tmp_path, client=FakeClient(markets={START + 300: FakeMarket(START + 300)}),
                   clock=clock)
    assert w.run(windows=1) == 3
    rows = read_rows(w.out)
    assert {r["window_start"] for r in rows} == {START + 300}
    assert clock.slept[0] == pytest.approx(265.0)      # to the next window's +5s


def test_a_session_of_several_windows_walks_forward(tmp_path):
    markets = {START + 300 * k: FakeMarket(START + 300 * k) for k in range(3)}
    w, _ = watcher(tmp_path, client=FakeClient(markets=markets))
    assert w.run(windows=3) == 9
    starts = sorted({r["window_start"] for r in read_rows(w.out)})
    assert starts == [START, START + 300, START + 600]


def test_the_market_and_the_bars_are_fetched_once_per_window(tmp_path):
    """Three readings of one window must not be three fetches of everything: the
    market does not change inside its window and neither does the closed bar."""
    calls = {"n": 0}
    series = bars()

    def feed():
        calls["n"] += 1
        return series

    client = FakeClient()
    w, _ = watcher(tmp_path, client=client, feed=feed)
    w.run(windows=1)
    assert client.market_calls == [START]
    assert calls["n"] == 1
    assert len(client.book_calls) == 6                 # both sides, every offset


def test_offsets_are_validated(tmp_path):
    with pytest.raises(ValueError, match="inside the window"):
        watcher(tmp_path, offsets=(5, 300))
    with pytest.raises(ValueError, match="inside the window"):
        watcher(tmp_path, offsets=(-1,))
    with pytest.raises(ValueError, match="at least one offset"):
        watcher(tmp_path, offsets=())


# --------------------------------------------------------------------------- #
# one reading
# --------------------------------------------------------------------------- #


def test_a_reading_records_both_books_and_the_bar(tmp_path):
    w, _ = watcher(tmp_path)
    obs = w.observe(START, 5)
    assert obs.slug == f"btc-updown-5m-{START}"
    assert (obs.up_bid, obs.up_ask) == (0.50, 0.51)
    assert (obs.down_bid, obs.down_ask) == (0.48, 0.49)
    assert obs.up_ask_depth == 100.0 and obs.down_ask_depth == 100.0
    assert obs.overround == pytest.approx(0.0)         # 0.51 + 0.49 - 1
    assert obs.skew == pytest.approx(0.01)
    assert obs.bar_ts == START and obs.bar_lag == 0
    assert obs.bar_return is not None
    assert obs.side in ("UP", "DOWN", "FLAT") and obs.note == ""


def test_a_lagging_bar_feed_is_recorded_not_hidden(tmp_path):
    """The bar closing at the window's start is the one the strategy fades. A
    feed one bar behind is measuring the previous window, and must say so."""
    stale = bars(end=START - 300)
    w, _ = watcher(tmp_path, feed=lambda: stale)
    obs = w.observe(START, 5)
    assert obs.bar_ts == START - 300 and obs.bar_lag == 300


def test_a_window_with_no_market_still_writes_a_row(tmp_path):
    w, _ = watcher(tmp_path, client=FakeClient(markets={}))
    obs = w.observe(START, 5)
    assert obs.note == "no market at that slug"
    assert obs.up_ask is None and obs.slug == ""
    # The engine's verdict is still recorded: the bars were fine.
    assert obs.bar_ts == START
    assert len(read_rows(w.out)) == 1


def test_a_venue_error_is_recorded_not_raised(tmp_path):
    w, _ = watcher(tmp_path, client=FakeClient(market_error="gamma returned 502"))
    assert w.observe(START, 5).note.startswith("market: gamma returned 502")
    w2, _ = watcher(tmp_path, client=FakeClient(book_error="clob returned 404"))
    obs = w2.observe(START, 5)
    assert obs.note.startswith("book: clob returned 404")
    assert obs.up_ask is None and obs.price is None


def test_a_dead_bar_feed_is_recorded_not_raised(tmp_path):
    def boom():
        raise RuntimeError("could not reach binance (451)")

    w, _ = watcher(tmp_path, feed=boom)
    obs = w.observe(START, 5)
    assert obs.note.startswith("bars: could not reach binance")
    assert obs.bar_ts is None and obs.side == ""
    # The book is still recorded: half a row beats none.
    assert obs.up_ask == 0.51


def test_a_side_with_no_asks_leaves_no_price_but_keeps_the_signal(tmp_path):
    client = FakeClient(books={UP_TOKEN: FakeBook(0.50, None),
                               DOWN_TOKEN: FakeBook(0.48, 0.49)})
    w, _ = watcher(tmp_path, client=client)
    obs = w.observe(START, 5)
    assert obs.note == "a side has no asks"
    assert obs.up_ask is None and obs.overround is None
    assert obs.price is None and obs.edge is None
    assert obs.side in ("UP", "DOWN", "FLAT")          # the engine still ran


def test_a_market_that_is_not_up_down_is_refused_by_name(tmp_path):
    client = FakeClient(markets={START: FakeMarket(outcomes=("Yes", "No"))})
    w, _ = watcher(tmp_path, client=client)
    assert "not Up/Down" in w.observe(START, 5).note


def test_too_little_history_says_warm_up(tmp_path):
    w, _ = watcher(tmp_path, feed=lambda: bars(n=40))
    obs = w.observe(START, 5)
    assert "warm-up" in obs.note and obs.side == ""


def test_a_fired_signal_is_priced_at_the_real_ask(tmp_path):
    """The whole point: the side the stack wants, priced at what the book asks
    for it, with the venue's fee on top.

    The fee here is Polymarket's exact per-share charge, 0.07 x p x (1 - p),
    not the flat 175 bps the backtest approximates it with -- the live path
    knows the price, so it can charge what the venue charges."""
    w, _ = watcher(tmp_path)

    def planted(fs, i, now_ts=None):
        return Signal(bar_index=i, ts=int(fs.series.ts[i]), price=100.0, side=UP,
                      conviction=0.95, p_model=0.58, break_even=0.5175,
                      edge=0.0625, expected_value=0.1, tradable=True)

    w.engine.evaluate = planted
    obs = w.observe(START, 5)
    assert obs.side == "UP" and obs.tradable == "yes"
    assert obs.p_model == 0.58 and obs.conviction == 0.95
    fee = 0.07 * 0.51 * (1.0 - 0.51)                # 1.7493 cents, not a flat 1.75
    assert obs.price == pytest.approx(0.51 + fee, abs=1e-6)
    assert obs.edge == pytest.approx(0.58 - (0.51 + fee), abs=1e-6)
    assert obs.approved in ("yes", "no")
    assert obs.stake is not None


def test_a_flat_signal_records_no_price(tmp_path):
    w, _ = watcher(tmp_path)

    def flat(fs, i, now_ts=None):
        return Signal(bar_index=i, ts=int(fs.series.ts[i]), price=100.0, side=FLAT,
                      conviction=0.0, p_model=0.5, break_even=0.5175, edge=-0.0175,
                      expected_value=-0.1, tradable=False)

    w.engine.evaluate = flat
    obs = w.observe(START, 5)
    assert obs.side == "FLAT" and obs.tradable == "no"
    assert obs.price is None and obs.edge is None
    assert obs.reason.startswith("NO BET")


def test_the_real_engine_runs_end_to_end_on_synthetic_bars(tmp_path):
    """No fake signal: the configured stack, the features and the priced
    decision, all the way to a CSV row."""
    w, _ = watcher(tmp_path, cfg=config(
        gate_stack=["data_integrity", "mean_reversion", "volatility_regime"]))
    obs = w.observe(START, 5)
    assert obs.side in ("UP", "DOWN", "FLAT")
    assert obs.p_model is not None and obs.conviction is not None
    assert obs.note == ""
    row = read_rows(w.out)[0]
    assert row["side"] == obs.side and row["window_start"] == START


# --------------------------------------------------------------------------- #
# the CSV
# --------------------------------------------------------------------------- #


def test_the_header_is_written_once_and_rows_append(tmp_path):
    path = tmp_path / "q.csv"
    for offset in (5, 15):
        append_row(path, Observation(window_start=START, window_time="t",
                                     offset=offset, observed_ts=START + offset))
    text = path.read_text()
    assert text.count("window_start") == 1
    assert len(read_rows(path)) == 2
    # A second session continues the same file rather than truncating it.
    append_row(path, Observation(window_start=START + 300, window_time="t",
                                 offset=5, observed_ts=START + 305))
    assert len(read_rows(path)) == 3


def test_blanks_read_back_as_none_and_numbers_as_numbers(tmp_path):
    path = tmp_path / "q.csv"
    append_row(path, Observation(window_start=START, window_time="t", offset=5,
                                 observed_ts=START + 5, up_ask=0.51, bar_lag=0))
    row = read_rows(path)[0]
    assert row["up_ask"] == 0.51 and row["bar_lag"] == 0
    assert row["down_ask"] is None and row["settled"] == ""
    assert isinstance(row["window_start"], int)


# --------------------------------------------------------------------------- #
# settlement
# --------------------------------------------------------------------------- #


def rows_for(path, *windows, **kw):
    for start in windows:
        for offset in (5, 15):
            append_row(path, Observation(window_start=start, window_time="t",
                                         offset=offset, observed_ts=start + offset,
                                         **kw))
    return path


def test_settle_fills_ended_windows_only(tmp_path):
    path = rows_for(tmp_path / "q.csv", START, START + 300)
    client = FakeClient(markets={START: FakeMarket(START, settled="Down"),
                                 START + 300: FakeMarket(START + 300)})
    # Only the first window has ended.
    filled, left = settle(client, path, now=START + 400)
    assert (filled, left) == (1, 0)
    rows = read_rows(path)
    assert [r["settled"] for r in rows] == ["Down", "Down", "", ""]
    assert client.market_calls == [START]              # the open one is not asked about


def test_settle_asks_once_per_window_and_is_idempotent(tmp_path):
    path = rows_for(tmp_path / "q.csv", START)
    client = FakeClient(markets={START: FakeMarket(START, settled="Up")})
    assert settle(client, path, now=START + 400) == (1, 0)
    assert client.market_calls == [START]
    # Already settled: nothing re-read, nothing rewritten.
    assert settle(client, path, now=START + 400) == (0, 0)
    assert client.market_calls == [START]
    assert [r["settled"] for r in read_rows(path)] == ["Up", "Up"]


def test_an_ended_window_the_venue_has_not_resolved_is_counted_as_left(tmp_path):
    path = rows_for(tmp_path / "q.csv", START)
    client = FakeClient(markets={START: FakeMarket(START, settled=None)})
    assert settle(client, path, now=START + 400) == (0, 1)
    client = FakeClient(market_error="gamma returned 502")
    assert settle(client, path, now=START + 400) == (0, 1)


def test_settle_on_an_empty_file_does_nothing(tmp_path):
    path = tmp_path / "q.csv"
    append_row(path, Observation(window_start=START, window_time="t", offset=5,
                                 observed_ts=START))
    path.write_text(path.read_text().splitlines()[0] + "\n")     # header only
    assert settle(FakeClient(), path, now=START + 400) == (0, 0)


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def observation(**over):
    base = dict(window_start=START, window_time="t", offset=5, observed_ts=START + 5,
                slug="s", up_bid=0.50, up_ask=0.51, down_bid=0.48, down_ask=0.49,
                up_ask_depth=100.0, down_ask_depth=80.0, overround=0.0, skew=0.01,
                bar_ts=START, bar_lag=0, bar_return=-0.004, side="UP",
                conviction=0.9, p_model=0.56, tradable="yes", price=0.5275,
                edge=0.0325, approved="yes", stake=7.5, reason="UP", settled="",
                note="")
    base.update(over)
    return base


def test_the_report_splits_the_quote_by_whether_the_gates_fired():
    rows = [observation(), observation(offset=15, skew=0.20, up_ask=0.70,
                                       down_ask=0.31, price=0.7175, edge=-0.1575),
            observation(offset=30, tradable="no", side="FLAT", price=None,
                        edge=None, approved="no", skew=0.02)]
    text = render_report(rows, break_even=0.5175)
    assert "rows 3   windows 1" in text
    assert "both sides priced 3" in text
    assert "+  5s" in text and "+ 15s" in text and "+ 30s" in text
    assert "gates fired   2" in text and "no signal     1" in text
    assert "a positive edge at the real price: 1/2" in text
    assert "break-even at this venue: 0.5175" in text
    assert "within 5 points of even: 2/3" in text


def test_the_report_counts_coverage_problems():
    rows = [observation(), observation(offset=15, up_ask=None, down_ask=None,
                                       note="a side has no asks", price=None, edge=None),
            observation(offset=30, bar_lag=300)]
    text = render_report(rows)
    assert "both sides priced 2" in text
    assert "rows with a note 1" in text
    assert "bar lag on 1" in text


def test_the_report_scores_settled_bets_at_the_price_actually_paid():
    """Two bets at 0.5275: one right, one wrong. The winner returns 1 - price and
    the loser costs the price, so the pair is a small net loss at this quote."""
    rows = [observation(settled="UP"),
            observation(window_start=START + 300, offset=5, settled="DOWN")]
    text = render_report(rows, break_even=0.5175)
    assert "bets 2   right 1 (50.0%)" in text
    assert "P&L per contract -0.0275" in text          # ((1-0.5275) + (-0.5275)) / 2
    assert "anecdote" in text                          # the sample-size warning


def test_the_report_says_when_nothing_has_settled():
    text = render_report([observation()])
    assert "none yet" in text and "btc5m settle" in text


def test_the_report_handles_an_empty_file():
    assert render_report([]) == "no rows"
