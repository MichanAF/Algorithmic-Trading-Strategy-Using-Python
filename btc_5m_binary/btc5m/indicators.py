"""Vectorised indicator primitives for 5-minute bar series.

Every public function takes 1-D float arrays and returns an array of the same
length, left-padded with NaN for the warm-up period.  Keeping the shape stable
means a gate can always index a feature by bar number without bookkeeping.

Only numpy is used on purpose: the live engine recomputes features over a
trailing window on every bar, so the primitives need to be cheap and free of
any framework-version drift.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma", "ema", "rma", "rolling_sum", "rolling_std", "rolling_median",
    "rolling_max", "rolling_min", "rolling_rank", "zscore",
    "true_range", "atr", "rsi", "macd", "roc", "adx",
    "bollinger_width", "linreg_slope", "linreg_r2", "obv",
    "rolling_vwap", "variance_ratio", "body_dominance", "resample_ohlcv",
]


def _as_float(x) -> np.ndarray:
    arr = np.asarray(x, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"expected a 1-D series, got shape {arr.shape}")
    return arr


def _windows(x: np.ndarray, n: int) -> np.ndarray:
    """Sliding windows of width n; shape (len(x) - n + 1, n)."""
    return np.lib.stride_tricks.sliding_window_view(x, n)


def _blank(x: np.ndarray) -> np.ndarray:
    return np.full(x.shape, np.nan, dtype=float)


def _rolling_reduce(x, n: int, fn) -> np.ndarray:
    """Apply a numpy reduction over trailing windows of width n."""
    x = _as_float(x)
    out = _blank(x)
    if n <= 0:
        raise ValueError("window must be positive")
    if len(x) < n:
        return out
    out[n - 1:] = fn(_windows(x, n), axis=-1)
    return out


# --------------------------------------------------------------------------- #
# moving averages
# --------------------------------------------------------------------------- #

def sma(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, np.mean)


def ema(x, n: int) -> np.ndarray:
    """Exponential MA seeded with the first n-bar SMA (standard TA convention)."""
    x = _as_float(x)
    out = _blank(x)
    if len(x) < n:
        return out
    alpha = 2.0 / (n + 1.0)
    acc = float(np.mean(x[:n]))
    out[n - 1] = acc
    for i in range(n, len(x)):
        acc = alpha * x[i] + (1.0 - alpha) * acc
        out[i] = acc
    return out


def rma(x, n: int) -> np.ndarray:
    """Wilder's smoothing (alpha = 1/n), used by RSI/ATR/ADX."""
    x = _as_float(x)
    out = _blank(x)
    if len(x) < n:
        return out
    acc = float(np.mean(x[:n]))
    out[n - 1] = acc
    for i in range(n, len(x)):
        acc = (acc * (n - 1) + x[i]) / n
        out[i] = acc
    return out


# --------------------------------------------------------------------------- #
# rolling statistics
# --------------------------------------------------------------------------- #

def rolling_sum(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, np.sum)


def rolling_std(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, lambda w, axis: np.std(w, axis=axis, ddof=1))


def rolling_median(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, np.median)


def rolling_max(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, np.max)


def rolling_min(x, n: int) -> np.ndarray:
    return _rolling_reduce(x, n, np.min)


def rolling_rank(x, n: int) -> np.ndarray:
    """Percentile rank of the newest value inside its own trailing window.

    Returns a value in [0, 1]: 0.9 means "higher than 90% of the last n bars".
    This is how the volatility gate stays self-calibrating -- an absolute ATR
    threshold rots as BTC's price level changes, a percentile does not.
    """
    x = _as_float(x)
    out = _blank(x)
    if len(x) < n:
        return out
    w = _windows(x, n)
    out[n - 1:] = (w <= w[:, -1:]).sum(axis=-1) / float(n)
    return out


def zscore(x, n: int) -> np.ndarray:
    x = _as_float(x)
    mu = sma(x, n)
    sd = rolling_std(x, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(sd > 0, (x - mu) / sd, 0.0)
    out[np.isnan(mu) | np.isnan(sd)] = np.nan
    return out


# --------------------------------------------------------------------------- #
# volatility
# --------------------------------------------------------------------------- #

def true_range(high, low, close) -> np.ndarray:
    high, low, close = _as_float(high), _as_float(low), _as_float(close)
    prev = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def bollinger_width(close, n: int = 20, k: float = 2.0) -> np.ndarray:
    """Band width as a fraction of the mid price -- a squeeze/expansion meter."""
    close = _as_float(close)
    mid = sma(close, n)
    sd = rolling_std(close, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(mid > 0, 2.0 * k * sd / mid, np.nan)


# --------------------------------------------------------------------------- #
# momentum
# --------------------------------------------------------------------------- #

def rsi(close, n: int = 14) -> np.ndarray:
    close = _as_float(close)
    delta = np.diff(close, prepend=close[0])
    gain = rma(np.clip(delta, 0.0, None), n)
    loss = rma(np.clip(-delta, 0.0, None), n)
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = np.where(loss > 0, gain / loss, np.inf)
        out = 100.0 - 100.0 / (1.0 + rs)
    out[np.isnan(gain) | np.isnan(loss)] = np.nan
    return out


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    line = ema(close, fast) - ema(close, slow)
    valid = ~np.isnan(line)
    sig = _blank(line)
    if valid.any():
        start = int(np.argmax(valid))
        sig[start:] = ema(line[start:], signal)
    return line, sig, line - sig


def roc(close, n: int = 3) -> np.ndarray:
    """n-bar rate of change as a fraction (0.004 == +0.4%)."""
    close = _as_float(close)
    out = _blank(close)
    if len(close) <= n:
        return out
    base = close[:-n]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[n:] = np.where(base > 0, (close[n:] - base) / base, np.nan)
    return out


def adx(high, low, close, n: int = 14):
    """Returns (adx, plus_di, minus_di).  Wilder's directional movement."""
    high, low, close = _as_float(high), _as_float(low), _as_float(close)
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr_n = rma(true_range(high, low, close), n)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = np.where(tr_n > 0, 100.0 * rma(plus_dm, n) / tr_n, np.nan)
        minus_di = np.where(tr_n > 0, 100.0 * rma(minus_dm, n) / tr_n, np.nan)
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    valid = ~np.isnan(dx)
    out = _blank(dx)
    if valid.any():
        start = int(np.argmax(valid))
        out[start:] = rma(np.nan_to_num(dx[start:]), n)
    return out, plus_di, minus_di


# --------------------------------------------------------------------------- #
# trend shape
# --------------------------------------------------------------------------- #

def _linreg(x: np.ndarray, n: int):
    x = _as_float(x)
    slope, r2 = _blank(x), _blank(x)
    if len(x) < n or n < 3:
        return slope, r2
    t = np.arange(n, dtype=float)
    t_c = t - t.mean()
    t_ss = float((t_c ** 2).sum())
    w = _windows(x, n)
    w_c = w - w.mean(axis=-1, keepdims=True)
    beta = (w_c * t_c).sum(axis=-1) / t_ss
    ss_tot = (w_c ** 2).sum(axis=-1)
    ss_res = ((w_c - beta[:, None] * t_c) ** 2).sum(axis=-1)
    slope[n - 1:] = beta
    with np.errstate(invalid="ignore", divide="ignore"):
        r2[n - 1:] = np.where(ss_tot > 0, 1.0 - ss_res / ss_tot, 0.0)
    return slope, r2


def linreg_slope(x, n: int = 20) -> np.ndarray:
    """Least-squares slope in price units per bar."""
    return _linreg(x, n)[0]


def linreg_r2(x, n: int = 20) -> np.ndarray:
    """Goodness of fit of that line -- how orderly the move is, 0..1."""
    return _linreg(x, n)[1]


def variance_ratio(close, n: int = 60, q: int = 5) -> np.ndarray:
    """Lo-MacKinlay variance ratio on log returns over a trailing window.

    VR > 1 means q-bar moves are larger than q independent 1-bar moves would
    be, i.e. returns trend and momentum entries are the right tool.  VR < 1
    means they mean-revert and you should be fading, not following.  This is
    the cheap stand-in for a rolling Hurst exponent.
    """
    close = _as_float(close)
    out = _blank(close)
    with np.errstate(invalid="ignore", divide="ignore"):
        logp = np.log(np.where(close > 0, close, np.nan))
    r = np.diff(logp, prepend=np.nan)
    if len(r) < n + 1 or q < 2:
        return out
    w = _windows(r[1:], n)
    var1 = np.var(w, axis=-1, ddof=1)
    # q-bar returns built from the same window, non-overlapping.
    usable = (n // q) * q
    blocks = w[:, n - usable:].reshape(w.shape[0], usable // q, q).sum(axis=-1)
    varq = np.var(blocks, axis=-1, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = np.where(var1 > 0, varq / (q * var1), np.nan)
    out[n:] = vr
    return out


def body_dominance(open_, high, low, close) -> np.ndarray:
    """Signed candle body as a fraction of its full range, in [-1, 1].

    +0.8 is a decisive up bar that closed near its high; +0.1 is a doji that
    happened to close green.  Momentum gates should not treat them alike.
    """
    open_, high, low, close = map(_as_float, (open_, high, low, close))
    rng = high - low
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(rng > 0, (close - open_) / rng, 0.0)


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #

def obv(close, volume) -> np.ndarray:
    close, volume = _as_float(close), _as_float(volume)
    sign = np.sign(np.diff(close, prepend=close[0]))
    return np.cumsum(sign * volume)


def rolling_vwap(high, low, close, volume, n: int = 24) -> np.ndarray:
    """Volume-weighted average price over a trailing window of n bars.

    Rolling rather than session-anchored: crypto has no session close, and a
    2-hour (24 x 5m) anchor is what intraday BTC flow actually respects.
    """
    high, low, close, volume = map(_as_float, (high, low, close, volume))
    typical = (high + low + close) / 3.0
    pv = rolling_sum(typical * volume, n)
    v = rolling_sum(volume, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(v > 0, pv / v, np.nan)


# --------------------------------------------------------------------------- #
# timeframe aggregation
# --------------------------------------------------------------------------- #

def resample_ohlcv(ts, open_, high, low, close, volume, factor: int):
    """Aggregate 5-minute bars into higher-timeframe bars.

    Returns (index, ts, o, h, l, c, v) where ``index`` maps each aggregated bar
    to the 5-minute bar that closed it.  The higher-timeframe gate looks up
    ``index`` so it can only ever read a bar that has already completed -- the
    usual source of look-ahead bias in multi-timeframe systems.
    """
    ts = np.asarray(ts)
    open_, high, low, close, volume = map(_as_float, (open_, high, low, close, volume))
    if factor < 1:
        raise ValueError("factor must be >= 1")
    n = (len(close) // factor) * factor
    if n == 0:
        empty = np.array([], dtype=float)
        return (np.array([], dtype=int), np.array([], dtype=ts.dtype),
                empty, empty, empty, empty, empty)
    shape = (n // factor, factor)
    idx = np.arange(factor - 1, n, factor)
    return (
        idx,
        ts[idx],
        open_[:n].reshape(shape)[:, 0],
        high[:n].reshape(shape).max(axis=1),
        low[:n].reshape(shape).min(axis=1),
        close[:n].reshape(shape)[:, -1],
        volume[:n].reshape(shape).sum(axis=1),
    )
