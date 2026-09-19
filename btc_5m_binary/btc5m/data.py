"""Bar series container plus the three ways to get 5-minute BTC bars.

``load_csv``    -- whatever you already have on disk or exported from an exchange.
``fetch_klines``-- pull live 5m candles straight from a public exchange endpoint.
``synthetic``   -- a seeded generator, for tests and for sanity-checking the
                   gate stack when the network is unavailable.
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BAR_SECONDS = 300

# Kline intervals the Binance paths can fetch.  Everything downstream reasons
# in 5-minute bars; the 1-minute interval exists for one purpose -- measuring
# who was aggressing in the last minute or three before a window opens -- and
# is consumed only through ``--minute``, never as the series a strategy runs on.
INTERVAL_SECONDS = {"1m": 60, "5m": 300}


def _bar_seconds_for(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError:
        raise ValueError(f"unsupported interval {interval!r}; "
                         f"choose from {sorted(INTERVAL_SECONDS)}") from None


@dataclass
class BarSeries:
    """Closed 5-minute OHLCV bars.  ``ts`` is the bar's close time, epoch seconds."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    spread_bps: np.ndarray | None = None
    # Taker-buy base volume per bar: the share of each bar's volume that was
    # aggressive buying.  Binance publishes it in every kline; other exchanges
    # do not, and synthetic bars have none, so it is optional like spread_bps.
    taker_buy: np.ndarray | None = None
    symbol: str = "BTCUSDT"

    def __post_init__(self) -> None:
        self.ts = np.asarray(self.ts, dtype=np.int64)
        for name in ("open", "high", "low", "close", "volume"):
            setattr(self, name, np.asarray(getattr(self, name), dtype=float))
        for name in ("spread_bps", "taker_buy"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, np.asarray(value, dtype=float))
        lengths = {len(self.ts), len(self.open), len(self.high),
                   len(self.low), len(self.close), len(self.volume)}
        if len(lengths) != 1:
            raise ValueError(f"ragged series: column lengths {sorted(lengths)}")
        for name in ("spread_bps", "taker_buy"):
            value = getattr(self, name)
            if value is not None and len(value) != len(self.ts):
                raise ValueError(f"{name} length does not match the series")
        if len(self.ts) > 1 and np.any(np.diff(self.ts) <= 0):
            raise ValueError("timestamps must be strictly increasing")

    def __len__(self) -> int:
        return len(self.ts)

    def tail(self, n: int) -> "BarSeries":
        return self[max(0, len(self) - n):]

    def __getitem__(self, sl: slice) -> "BarSeries":
        if not isinstance(sl, slice):
            raise TypeError("BarSeries supports slicing only")
        return BarSeries(
            ts=self.ts[sl], open=self.open[sl], high=self.high[sl],
            low=self.low[sl], close=self.close[sl], volume=self.volume[sl],
            spread_bps=None if self.spread_bps is None else self.spread_bps[sl],
            taker_buy=None if self.taker_buy is None else self.taker_buy[sl],
            symbol=self.symbol,
        )

    def time_at(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.ts[i]), tz=timezone.utc)

    @property
    def bar_seconds(self) -> int:
        """Bar length, inferred from the timestamps.

        A CSV does not say what interval it holds and the same loader reads a
        1-minute file and a 5-minute one, so the length is a fact of the data
        rather than a label to trust.  The smallest step is the bar length
        unless every step is a gap, so it is snapped to the coarsest known
        interval that divides it: two 5-minute bars ten bars apart are still
        5-minute bars.  Fewer than two bars: the package default.
        """
        if len(self) < 2:
            return BAR_SECONDS
        step = int(np.min(np.diff(self.ts)))
        for known in sorted(INTERVAL_SECONDS.values(), reverse=True):
            if step % known == 0:
                return known
        return step

    def gaps(self) -> list[tuple[int, int]]:
        """Missing stretches as (index of the bar after the gap, bars missing).

        Real exchange history has holes -- maintenance windows, outages, the odd
        delisted minute -- and they are not filled anywhere in this package.  A
        gap is a bar whose predecessor is more than one bar behind it, which is
        exactly what the data_integrity gate vetoes.  Worth printing after a
        download so a hole is a known fact rather than a surprise in the
        blocker table.
        """
        if len(self) < 2:
            return []
        step = self.bar_seconds
        steps = np.diff(self.ts)
        missing = np.nonzero(steps > step)[0]
        return [(int(i) + 1, int(steps[i] // step) - 1) for i in missing]

    def gap_count(self) -> int:
        return len(self.gaps())

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The taker column is written only when the series carries it, so a
        # CSV from a source without it stays byte-identical to before and a
        # reader never sees a column of NaN pretending to be data.
        with_taker = self.taker_buy is not None
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            header = ["timestamp", "open", "high", "low", "close", "volume"]
            w.writerow(header + (["taker_buy"] if with_taker else []))
            for i in range(len(self)):
                row = [
                    int(self.ts[i]),
                    f"{self.open[i]:.2f}", f"{self.high[i]:.2f}",
                    f"{self.low[i]:.2f}", f"{self.close[i]:.2f}",
                    f"{self.volume[i]:.6f}",
                ]
                if with_taker:
                    t = self.taker_buy[i]
                    row.append("" if np.isnan(t) else f"{t:.6f}")
                w.writerow(row)
        return path


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

_ALIASES = {
    "timestamp": "ts", "time": "ts", "date": "ts", "datetime": "ts",
    "open_time": "ts", "close_time": "ts", "opentime": "ts",
    "open": "open", "o": "open",
    "high": "high", "h": "high",
    "low": "low", "l": "low",
    "close": "close", "c": "close", "adj close": "close", "adj_close": "close",
    "volume": "volume", "v": "volume", "vol": "volume", "base_volume": "volume",
    "spread_bps": "spread_bps", "spread": "spread_bps",
    "taker_buy": "taker_buy", "taker_buy_base": "taker_buy",
    "taker_buy_base_asset_volume": "taker_buy", "taker_buy_volume": "taker_buy",
}


def _parse_timestamp(raw: str) -> int:
    raw = raw.strip()
    try:
        value = float(raw)
    except ValueError:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    # Exchange exports use seconds, milliseconds or microseconds interchangeably.
    while value > 1e11:
        value /= 1000.0
    return int(value)


def load_csv(path: str | Path, symbol: str = "BTCUSDT") -> BarSeries:
    """Read OHLCV bars from CSV, tolerating the usual column-name variations."""
    path = Path(path)
    with path.open(newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError(f"{path} is empty")

    header = [_ALIASES.get(h.strip().lower(), None) for h in rows[0]]
    if "close" not in header:
        raise ValueError(
            f"{path}: no recognisable close column in header {rows[0]!r}"
        )
    cols: dict[str, list] = {k: [] for k in ("ts", "open", "high", "low", "close",
                                             "volume", "spread_bps", "taker_buy")}
    for line_no, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        for name, cell in zip(header, row):
            if name is None or name not in cols:
                continue
            cell = cell.strip()
            if not cell:
                cols[name].append(np.nan)
            elif name == "ts":
                cols[name].append(_parse_timestamp(cell))
            else:
                try:
                    cols[name].append(float(cell))
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_no}: bad {name} value {cell!r}") from exc

    n = len(cols["close"])
    if n == 0:
        raise ValueError(f"{path}: header only, no data rows")
    close = np.asarray(cols["close"], dtype=float)
    ts = (np.asarray(cols["ts"], dtype=np.int64) if len(cols["ts"]) == n
          else np.arange(n, dtype=np.int64) * BAR_SECONDS)

    def column(name: str, fallback: np.ndarray) -> np.ndarray:
        return np.asarray(cols[name], dtype=float) if len(cols[name]) == n else fallback

    return BarSeries(
        ts=ts,
        open=column("open", np.concatenate(([close[0]], close[:-1]))),
        high=column("high", close),
        low=column("low", close),
        close=close,
        volume=column("volume", np.ones(n)),
        spread_bps=np.asarray(cols["spread_bps"], dtype=float) if len(cols["spread_bps"]) == n else None,
        taker_buy=np.asarray(cols["taker_buy"], dtype=float) if len(cols["taker_buy"]) == n else None,
        symbol=symbol,
    )


# --------------------------------------------------------------------------- #
# live fetch
# --------------------------------------------------------------------------- #

_ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
    # Binance's public data mirror.  Same payload, same fields -- including the
    # taker-buy volume the flow gate needs -- but not geo-restricted, so it
    # answers from a US IP where api.binance.com returns 451.  The only live
    # source a watcher on a blocked network has.
    "binance-vision": "https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
    "coinbase": "https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=300",
    "kraken": "https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=5",
}

_DEFAULT_SYMBOLS = {"binance": "BTCUSDT", "binance-vision": "BTCUSDT",
                    "coinbase": "BTC-USD", "kraken": "XBTUSD"}

# The candle sources, for anything that offers them as a choice.  The CLI reads
# this rather than listing them again: a mirror added here but not there is a
# flag that parses everywhere except where it is needed, which is how
# binance-vision reached a workflow step that could not run it.
EXCHANGES = tuple(sorted(_ENDPOINTS))


def fetch_klines(exchange: str = "binance", symbol: str | None = None,
                 limit: int = 1000, timeout: int = 20,
                 interval: str = "5m") -> BarSeries:
    """Fetch recent closed candles from a public exchange endpoint.

    The in-progress candle is dropped: betting on a bar that has not closed is
    the most common way a 5-minute backtest quietly becomes fiction.  Only
    Binance serves an interval other than 5m here.
    """
    exchange = exchange.lower()
    if exchange not in _ENDPOINTS:
        raise ValueError(f"unsupported exchange {exchange!r}; "
                         f"choose from {sorted(_ENDPOINTS)}")
    bar_seconds = _bar_seconds_for(interval)
    if interval != "5m" and not exchange.startswith("binance"):
        raise ValueError(f"{exchange} is wired for 5m candles only; "
                         f"{interval} needs --exchange binance")
    symbol = symbol or _DEFAULT_SYMBOLS[exchange]
    url = _ENDPOINTS[exchange].format(symbol=symbol, limit=min(limit, 1000),
                                      interval=interval)

    request = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise RuntimeError(
            f"could not reach {exchange} ({exc}). Exchange endpoints are often "
            "blocked by corporate or sandbox network policy -- export a CSV and "
            "use --data instead, or run this from a machine with direct access."
        ) from exc

    rows = _parse_candles(exchange, payload, symbol, bar_seconds)

    if not rows:
        raise RuntimeError(f"{exchange} returned no candles for {symbol!r}")

    now = datetime.now(tz=timezone.utc).timestamp()
    rows = [r for r in rows if r[0] <= now][-limit:]
    if not rows:
        raise RuntimeError(f"{exchange} returned only unclosed candles")

    cols = list(zip(*rows))
    return BarSeries(ts=cols[0], open=cols[1], high=cols[2], low=cols[3],
                     close=cols[4], volume=cols[5], taker_buy=cols[6], symbol=symbol)


def _parse_candles(exchange: str, payload, symbol: str,
                   bar_seconds: int = BAR_SECONDS) -> list[tuple]:
    """One exchange's candle payload as ascending (close_ts, o, h, l, c, v) rows.

    Timestamps are **close** times, which is what the rest of this package means
    by a bar's ts.  Binance gives it directly; Coinbase and Kraken label a candle
    by its open, so a bar length is added.
    """
    # Every row is (close_ts, o, h, l, c, v, taker_buy).  Only Binance publishes
    # the taker-buy base volume -- field 9 of a kline -- so it is NaN elsewhere,
    # and NaN when a mirror or a test fake sends a short row.
    def taker(k) -> float:
        return float(k[9]) if len(k) > 9 else float("nan")

    if exchange.startswith("binance"):
        # Derived from openTime, not closeTime.  Binance's closeTime is the last
        # *millisecond* of the interval (openTime + 299999), so //1000 lands a
        # second short of the boundary and real bars would carry ...:04:59 where
        # synthetic and CSV bars carry ...:05:00.  A one-second skew is not
        # cosmetic here: predict.fun windows sit on exact 300-second grids, and
        # seconds_into_window is measured against a 30-second entry budget.
        rows = [(int(k[0]) // 1000 + bar_seconds, float(k[1]), float(k[2]),
                 float(k[3]), float(k[4]), float(k[5]), taker(k)) for k in payload]
    elif exchange == "coinbase":
        # [time, low, high, open, close, volume], newest first.
        rows = [(int(k[0]) + BAR_SECONDS, float(k[3]), float(k[2]), float(k[1]),
                 float(k[4]), float(k[5]), float("nan")) for k in payload]
    else:
        result = payload.get("result", {})
        if payload.get("error"):
            raise RuntimeError(f"kraken error: {payload['error']}")
        key = next((k for k in result if k != "last"), None)
        if key is None:
            raise RuntimeError(f"kraken returned no OHLC data for {symbol!r}")
        rows = [(int(k[0]) + BAR_SECONDS, float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[6]), float("nan")) for k in result[key]]
    rows.sort(key=lambda r: r[0])
    return rows


# How many candles one request returns, and how to ask for an earlier page.
# Kraken is absent on purpose: its public OHLC endpoint serves only the most
# recent ~720 candles whatever you pass, so it cannot supply a year.
_HISTORY_PAGES = {
    "binance": ("https://api.binance.com/api/v3/klines"
                "?symbol={symbol}&interval={interval}&limit=1000&endTime={end_ms}", 1000),
    "coinbase": ("https://api.exchange.coinbase.com/products/{symbol}/candles"
                 "?granularity=300&start={start_iso}&end={end_iso}", 300),
}


def fetch_history(exchange: str = "binance", symbol: str | None = None,
                  bars: int = 105_120, timeout: int = 20,
                  pause_seconds: float = 0.25, end_ts: int | None = None,
                  progress=None, interval: str = "5m") -> BarSeries:
    """Page backwards through a public endpoint until ``bars`` candles are held.

    ``fetch_klines`` is capped at one request, which is 1000 bars -- about three
    and a half days.  Every backtest number in the README came off a synthetic
    generator because of that gap, and a year of real history is the one thing
    that can tell you whether the edge is real.  A year of 5-minute bars is
    105,120 of them: 106 requests to Binance, 351 to Coinbase.

    Pages are walked from newest to oldest, deduplicated by close time, and
    returned ascending.  Paging stops early when the exchange runs out of
    history, so a symbol younger than ``bars`` yields what exists rather than
    looping.  Gaps are not filled -- ``load_csv`` and the data_integrity gate
    are what judge them, and inventing bars to paper over a venue outage is how
    a backtest starts lying.
    """
    exchange = exchange.lower()
    if exchange == "kraken":
        raise ValueError(
            "kraken's public OHLC endpoint returns only the most recent ~720 "
            "candles, so it cannot supply deep history. Use binance (or "
            "coinbase, if binance.com is geo-blocked where you are).")
    if exchange not in _HISTORY_PAGES:
        raise ValueError(f"no history endpoint for {exchange!r}; "
                         f"choose from {sorted(_HISTORY_PAGES)}")
    if bars < 1:
        raise ValueError(f"bars must be positive, got {bars}")
    bar_seconds = _bar_seconds_for(interval)
    if interval != "5m" and not exchange.startswith("binance"):
        raise ValueError(f"{exchange} is wired for 5m candles only; "
                         f"{interval} needs --exchange binance")

    symbol = symbol or _DEFAULT_SYMBOLS[exchange]
    template, page_size = _HISTORY_PAGES[exchange]
    now = int(datetime.now(tz=timezone.utc).timestamp())
    cursor = end_ts if end_ts is not None else now

    collected: dict[int, tuple] = {}
    # Generous but finite: enough pages for the request plus slack for gaps,
    # so a misbehaving endpoint cannot spin forever.
    for _ in range(bars // page_size + 8):
        span = page_size * bar_seconds
        url = template.format(
            symbol=symbol, end_ms=cursor * 1000, interval=interval,
            start_iso=datetime.fromtimestamp(cursor - span, tz=timezone.utc).isoformat(),
            end_iso=datetime.fromtimestamp(cursor, tz=timezone.utc).isoformat())
        request = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if collected:
                raise RuntimeError(
                    f"{exchange} stopped responding after {len(collected)} of "
                    f"{bars} bars ({exc}). Rerun to resume, or pass a smaller "
                    "--limit.") from exc
            raise RuntimeError(
                f"could not reach {exchange} ({exc}). Exchange endpoints are "
                "often blocked by corporate or sandbox network policy, and "
                "binance.com is geo-blocked in some countries -- try "
                "--exchange coinbase, or run this from a machine with direct "
                "access.") from exc

        rows = _parse_candles(exchange, payload, symbol, bar_seconds)
        fresh = [r for r in rows if r[0] not in collected and r[0] <= now]
        if not fresh:
            break                           # no history left before the cursor
        for row in fresh:
            collected[row[0]] = row
        if progress is not None:
            progress(len(collected), bars)
        if len(collected) >= bars:
            break
        # Step the cursor to just before the oldest bar held, so the next page
        # cannot repeat this one.
        cursor = min(collected) - bar_seconds
        if pause_seconds:
            time.sleep(pause_seconds)

    if not collected:
        raise RuntimeError(f"{exchange} returned no closed candles for {symbol!r}")

    ordered = [collected[ts] for ts in sorted(collected)][-bars:]
    cols = list(zip(*ordered))
    return BarSeries(ts=cols[0], open=cols[1], high=cols[2], low=cols[3],
                     close=cols[4], volume=cols[5], taker_buy=cols[6], symbol=symbol)


# Binance's public data dumps.  Not the trading API: these are static archives
# on a CDN, which matters for two reasons.  api.binance.com answers a US IP with
# HTTP 451 and GitHub's hosted runners are mostly US, so the API is the path that
# fails there; and a year of 5-minute bars is 12 zip files here against 106
# paged requests, with no rate limit and a byte-identical result every run.
DUMP_HOST = "https://data.binance.vision"
_DUMP_MONTH = DUMP_HOST + "/data/spot/monthly/klines/{sym}/{iv}/{sym}-{iv}-{ym}.zip"
_DUMP_DAY = DUMP_HOST + "/data/spot/daily/klines/{sym}/{iv}/{sym}-{iv}-{ymd}.zip"


def _dump_rows(blob: bytes, symbol: str,
               bar_seconds: int = BAR_SECONDS) -> list[tuple]:
    """Parse one Binance kline zip into ascending close-time rows.

    Two details the archives changed without renaming anything, both of which
    silently corrupt a series if assumed away:

    * **Timestamps switched from milliseconds to microseconds** in the 2025
      archives, so the unit is detected by magnitude rather than trusted.
    * **A header row appeared**, so the first line is skipped when it is not a
      number.

    Close time is derived from open time plus a bar length, matching the REST
    parser: the archives' own close_time column is the last microsecond of the
    interval and would land a tick short of the boundary.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n.endswith(".csv")]
        if not names:
            raise RuntimeError(
                f"{symbol} dump contained no CSV, only {archive.namelist()}")
        text = archive.read(names[0]).decode()

    rows = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            raw_open = float(parts[0])
        except ValueError:
            continue                      # the header row
        # Milliseconds are ~1.7e12, microseconds ~1.7e15.
        opened = int(raw_open / (1e6 if raw_open > 1e14 else 1e3))
        # Field 9 is taker_buy_base_volume, the one input in this package that
        # is not a function of price.  Kept, not discarded.
        taker = float(parts[9]) if len(parts) > 9 and parts[9] else float("nan")
        rows.append((opened + bar_seconds, float(parts[1]), float(parts[2]),
                     float(parts[3]), float(parts[4]), float(parts[5]), taker))
    rows.sort(key=lambda r: r[0])
    return rows


def _dump_periods(bars: int, now: datetime,
                  bar_seconds: int = BAR_SECONDS) -> tuple[list[str], list[str]]:
    """Which monthly and daily archives to pull to cover ``bars`` bars back.

    The current month has no monthly archive until it ends, so its elapsed days
    come from daily archives.  Yesterday is the newest complete day.
    """
    days_needed = bars * bar_seconds / 86_400
    months: list[str] = []
    cursor = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while len(months) * 28 < days_needed + 31:
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        months.append(cursor.strftime("%Y-%m"))
        if len(months) > 130:             # ~11 years, far past any sane request
            break
    months.reverse()

    days = [(datetime(now.year, now.month, 1, tzinfo=timezone.utc)
             + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((now - datetime(now.year, now.month, 1,
                                           tzinfo=timezone.utc)).days)]
    return months, days


def fetch_binance_dump(symbol: str = "BTCUSDT", bars: int = 105_120,
                       timeout: int = 60, pause_seconds: float = 0.0,
                       now: datetime | None = None, progress=None,
                       interval: str = "5m") -> BarSeries:
    """Build a series from Binance's public archives rather than its REST API.

    This is the preferred path for a backtest.  ``fetch_history`` pages the REST
    API, which needs 106 requests for a year and is refused outright from a US
    IP; the archives are 12 files, unmetered, and identical on every run.

    A missing archive is skipped rather than fatal -- Binance has occasional
    holes, and a month that does not exist for a young symbol is not an error.
    Gaps that result are reported by ``BarSeries.gaps()``, never filled.

    ``interval`` is ``"5m"`` for the series a strategy runs on and ``"1m"`` for
    the minute bars the taker-flow gate can read through ``--minute``; a year
    of the latter is 525,600 bars from the same twelve monthly archives.
    """
    if bars < 1:
        raise ValueError(f"bars must be positive, got {bars}")
    bar_seconds = _bar_seconds_for(interval)
    now = now or datetime.now(tz=timezone.utc)
    months, days = _dump_periods(bars, now, bar_seconds)
    urls = [_DUMP_MONTH.format(sym=symbol, iv=interval, ym=m) for m in months]
    urls += [_DUMP_DAY.format(sym=symbol, iv=interval, ymd=d) for d in days]

    collected: dict[int, tuple] = {}
    for i, url in enumerate(urls):
        request = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                blob = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                continue                  # no archive for that period
            raise RuntimeError(
                f"{DUMP_HOST} returned {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if collected:
                raise RuntimeError(
                    f"{DUMP_HOST} stopped responding after {len(collected)} "
                    f"bars ({exc}). Rerun to resume.") from exc
            raise RuntimeError(
                f"could not reach {DUMP_HOST} ({exc}). Unlike api.binance.com "
                "this host is not geo-restricted, so a failure here is usually "
                "a proxy or firewall rather than your location.") from exc

        for row in _dump_rows(blob, symbol, bar_seconds):
            collected[row[0]] = row
        if progress is not None:
            progress(i + 1, len(urls), len(collected))
        if pause_seconds:
            time.sleep(pause_seconds)

    if not collected:
        raise RuntimeError(
            f"no archives found for {symbol!r} on {DUMP_HOST}. Check the symbol "
            f"spelling -- these are spot pairs, so BTCUSDT not BTC-USD or "
            f"BTC/USDT. Tried {len(urls)} periods.")

    now_ts = int(now.timestamp())
    ordered = [collected[ts] for ts in sorted(collected) if ts <= now_ts][-bars:]
    if not ordered:
        raise RuntimeError(f"{symbol} archives held only unclosed candles")
    cols = list(zip(*ordered))
    # A missing archive needs no separate channel: if it is in the middle of the
    # range BarSeries.gaps() reports it, and if it is at the old end the series
    # simply comes back shorter than asked for, which the caller can see.
    return BarSeries(ts=cols[0], open=cols[1], high=cols[2], low=cols[3],
                     close=cols[4], volume=cols[5], taker_buy=cols[6], symbol=symbol)


def fetch_topofbook_mid(symbol: str = "BTCUSDT", timeout: int = 10) -> dict:
    """Current Binance top-of-book bid, ask and mid.

    A venue settles on an oracle mid, not a last traded price, so a live decision
    should read a mid.  Which mid is the venue's business and can differ from
    this one: a live predict.fun crypto market declares **Pyth BTC/USD**, while
    Trust Wallet's rules text cites the Chainlink BTC/USDT top-of-book stream
    (which is the mid of Binance's best bid and ask -- what this returns).  Read
    the settling feed from the market itself with ``PredictMarket.feed``; treat
    this as a fast proxy for it, not as the settlement price.

    For *backtesting* the distinction does not matter: Binance's BTCUSDT spread
    is about one cent, so mid and last differ by at most half a cent, while the
    median five-minute move is tens of dollars.  Only about one bar in thirteen
    thousand moves less than half a spread.  Use klines for history and this for
    the live quote.
    """
    url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}"
    request = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise RuntimeError(
            f"could not reach Binance for the top of book ({exc}). Exchange "
            "endpoints are often blocked by network policy; read the mid from "
            "the Chainlink stream instead."
        ) from exc
    bid, ask = float(payload["bidPrice"]), float(payload["askPrice"])
    return {"symbol": symbol, "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
            "spread": ask - bid,
            "spread_bps": (ask - bid) / ((bid + ask) / 2.0) * 10_000.0,
            "observed_ts": int(datetime.now(tz=timezone.utc).timestamp())}


# --------------------------------------------------------------------------- #
# synthetic bars
# --------------------------------------------------------------------------- #

def synthetic(n: int = 6000, start_price: float = 60_000.0, seed: int = 7,
              start_ts: int = 1_700_000_000, symbol: str = "BTCUSDT",
              trend_ar: float = 0.06, chop_ar: float = -0.08,
              trend_probability: float = 0.55) -> BarSeries:
    """Seeded 5-minute bars with the features the gate stack is built to read.

    The generator mixes trending bursts with mean-reverting chop, and the
    defaults are calibrated so the lag-1 autocorrelation of 5-minute returns
    lands near -0.03 overall -- roughly what BTC actually shows -- while being
    positive (about +0.16) inside trends and negative (about -0.08) in chop.

    That mix is the point.  A fixture with strong unconditional momentum would
    flatter any trend-following stack, and one with strong unconditional mean
    reversion would condemn it.  This one pays only for correctly identifying
    which regime is currently running, which is the job the gates claim to do.

    ``trend_ar`` and ``chop_ar`` are exposed so a test can build a deliberately
    trending or deliberately choppy series and assert the stack responds.
    """
    rng = np.random.default_rng(seed)

    # Volatility clusters: an AR(1) process on log-vol, plus an intraday shape
    # that peaks around the US session.
    log_vol = np.zeros(n)
    log_vol[0] = np.log(0.0009)
    for i in range(1, n):
        log_vol[i] = 0.985 * log_vol[i - 1] + 0.015 * np.log(0.0009) + rng.normal(0, 0.06)
    hour = ((start_ts + np.arange(n) * BAR_SECONDS) // 3600) % 24
    seasonal = 0.75 + 0.5 * np.exp(-0.5 * ((hour - 15) / 5.0) ** 2)
    vol = np.exp(log_vol) * seasonal

    # Regime switch between trending drift and mean reversion.
    trending = np.zeros(n, dtype=bool)
    drift = np.zeros(n)
    state, remaining, direction = "chop", 0, 1
    for i in range(n):
        if remaining <= 0:
            if state == "chop" and rng.random() < trend_probability:
                state, remaining = "trend", int(rng.integers(18, 90))
                direction = 1 if rng.random() < 0.5 else -1
            else:
                state, remaining = "chop", int(rng.integers(40, 200))
        remaining -= 1
        trending[i] = state == "trend"
        drift[i] = direction * 0.30 * vol[i] if state == "trend" else 0.0

    shock = rng.standard_t(df=4, size=n) / np.sqrt(4 / 2.0)
    ret = drift + vol * shock
    # Chop mean-reverts on the previous bar; trends extend it.
    for i in range(1, n):
        ret[i] += (trend_ar if trending[i] else chop_ar) * ret[i - 1]

    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.concatenate(([start_price], close[:-1]))
    wick = np.abs(rng.normal(0, 0.45, n)) * vol * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.45, n)) * vol * close

    # Volume rises with realised move and with trend participation.
    base = 120.0 * seasonal
    volume = base * (1.0 + 4.0 * np.abs(ret) / np.maximum(vol, 1e-9) * 0.25)
    volume *= np.where(trending, 1.35, 1.0)
    volume *= np.exp(rng.normal(0, 0.25, n))

    return BarSeries(
        ts=start_ts + np.arange(n, dtype=np.int64) * BAR_SECONDS,
        open=open_, high=high, low=np.minimum(low, np.minimum(open_, close)),
        close=close, volume=volume, symbol=symbol,
    )
