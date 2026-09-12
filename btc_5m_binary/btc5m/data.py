"""Bar series container plus the three ways to get 5-minute BTC bars.

``load_csv``    -- whatever you already have on disk or exported from an exchange.
``fetch_klines``-- pull live 5m candles straight from a public exchange endpoint.
``synthetic``   -- a seeded generator, for tests and for sanity-checking the
                   gate stack when the network is unavailable.
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BAR_SECONDS = 300


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
    symbol: str = "BTCUSDT"

    def __post_init__(self) -> None:
        self.ts = np.asarray(self.ts, dtype=np.int64)
        for name in ("open", "high", "low", "close", "volume"):
            setattr(self, name, np.asarray(getattr(self, name), dtype=float))
        if self.spread_bps is not None:
            self.spread_bps = np.asarray(self.spread_bps, dtype=float)
        lengths = {len(self.ts), len(self.open), len(self.high),
                   len(self.low), len(self.close), len(self.volume)}
        if len(lengths) != 1:
            raise ValueError(f"ragged series: column lengths {sorted(lengths)}")
        if self.spread_bps is not None and len(self.spread_bps) != len(self.ts):
            raise ValueError("spread_bps length does not match the series")
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
            symbol=self.symbol,
        )

    def time_at(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.ts[i]), tz=timezone.utc)

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for i in range(len(self)):
                w.writerow([
                    int(self.ts[i]),
                    f"{self.open[i]:.2f}", f"{self.high[i]:.2f}",
                    f"{self.low[i]:.2f}", f"{self.close[i]:.2f}",
                    f"{self.volume[i]:.6f}",
                ])
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
                                             "volume", "spread_bps")}
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
        symbol=symbol,
    )


# --------------------------------------------------------------------------- #
# live fetch
# --------------------------------------------------------------------------- #

_ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit={limit}",
    "coinbase": "https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=300",
    "kraken": "https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=5",
}

_DEFAULT_SYMBOLS = {"binance": "BTCUSDT", "coinbase": "BTC-USD", "kraken": "XBTUSD"}


def fetch_klines(exchange: str = "binance", symbol: str | None = None,
                 limit: int = 1000, timeout: int = 20) -> BarSeries:
    """Fetch recent closed 5-minute candles from a public exchange endpoint.

    The in-progress candle is dropped: betting on a bar that has not closed is
    the most common way a 5-minute backtest quietly becomes fiction.
    """
    exchange = exchange.lower()
    if exchange not in _ENDPOINTS:
        raise ValueError(f"unsupported exchange {exchange!r}; "
                         f"choose from {sorted(_ENDPOINTS)}")
    symbol = symbol or _DEFAULT_SYMBOLS[exchange]
    url = _ENDPOINTS[exchange].format(symbol=symbol, limit=min(limit, 1000))

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

    if exchange == "binance":
        rows = [(int(k[6]) // 1000, float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[5])) for k in payload]
    elif exchange == "coinbase":
        # [time, low, high, open, close, volume], newest first.
        rows = [(int(k[0]) + BAR_SECONDS, float(k[3]), float(k[2]), float(k[1]),
                 float(k[4]), float(k[5])) for k in payload]
        rows.sort(key=lambda r: r[0])
    else:
        result = payload.get("result", {})
        if payload.get("error"):
            raise RuntimeError(f"kraken error: {payload['error']}")
        key = next((k for k in result if k != "last"), None)
        if key is None:
            raise RuntimeError(f"kraken returned no OHLC data for {symbol!r}")
        rows = [(int(k[0]) + BAR_SECONDS, float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[6])) for k in result[key]]

    if not rows:
        raise RuntimeError(f"{exchange} returned no candles for {symbol!r}")

    now = datetime.now(tz=timezone.utc).timestamp()
    rows = [r for r in rows if r[0] <= now][-limit:]
    if not rows:
        raise RuntimeError(f"{exchange} returned only unclosed candles")

    cols = list(zip(*rows))
    return BarSeries(ts=cols[0], open=cols[1], high=cols[2], low=cols[3],
                     close=cols[4], volume=cols[5], symbol=symbol)


def fetch_topofbook_mid(symbol: str = "BTCUSDT", timeout: int = 10) -> dict:
    """Current Binance top-of-book bid, ask and mid.

    This is the quantity the Chainlink BTC/USDT top-of-book stream publishes and
    the one venues settle against, so a live decision should read it rather than
    the last traded price.

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
