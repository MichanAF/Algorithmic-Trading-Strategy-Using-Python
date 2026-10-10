"""Turn a BarSeries into every factor the gate menu can ask for.

Features are computed once per run and indexed by bar, so a gate is a cheap
threshold check rather than a recalculation.  Two rules hold throughout:

* bar ``i`` may only read data known at the close of bar ``i``;
* higher-timeframe values are mapped from the last *completed* aggregate bar.

Those two rules are what keep a 5-minute backtest from quietly reading the
future, which is the failure mode that makes short-horizon systems look
profitable on paper and lose money live.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import indicators as ind
from .config import StrategyConfig
from .data import BAR_SECONDS, BarSeries


def _shift(x: np.ndarray, n: int = 1) -> np.ndarray:
    """Shift forward by n bars so bar i sees only bars up to i-n."""
    out = np.full_like(x, np.nan, dtype=float)
    if n < len(x):
        out[n:] = x[:len(x) - n]
    return out


def _slope(x: np.ndarray, lookback: int) -> np.ndarray:
    """Change over the last ``lookback`` bars, per bar."""
    return (x - _shift(x, lookback)) / float(lookback)


def _htf_to_base(values: np.ndarray, close_idx: np.ndarray, n: int) -> np.ndarray:
    """Map higher-timeframe values onto base-bar indices without look-ahead."""
    out = np.full(n, np.nan, dtype=float)
    if len(close_idx) == 0:
        return out
    pos = np.searchsorted(close_idx, np.arange(n), side="right") - 1
    valid = pos >= 0
    out[valid] = values[pos[valid]]
    return out


@dataclass
class FeatureSet:
    """All factor arrays for one series, aligned to the 5-minute bar index."""

    series: BarSeries
    values: dict[str, np.ndarray] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.series)

    def get(self, name: str, i: int) -> float:
        """Scalar factor value at bar i; NaN during warm-up."""
        try:
            return float(self.values[name][i])
        except KeyError as exc:
            raise KeyError(
                f"feature {name!r} was not computed; available: "
                f"{sorted(self.values)}"
            ) from exc

    def ready(self, i: int, names: tuple[str, ...]) -> bool:
        """True when every named factor has a finite value at bar i."""
        return all(np.isfinite(self.values[n][i]) for n in names)

    def warmup_bars(self, required: tuple[str, ...] | None = None) -> int:
        """First bar at which every *required* factor is finite.

        Defaults to every computed factor, but callers should pass the union of
        the active gates' inputs.  A factor no active gate reads -- the
        cross-asset reference, typically -- must not hold up the whole run.
        """
        names = tuple(self.values) if required is None else tuple(required)
        missing = [n for n in names if n not in self.values]
        if missing:
            raise KeyError(f"unknown feature(s) {missing}")
        n = len(self)
        for i in range(n):
            if all(np.isfinite(self.values[name][i]) for name in names):
                return i
        return n


def minute_taker_share(ts: np.ndarray, minute: BarSeries,
                       window: int) -> np.ndarray:
    """Taker-buy share over the last ``window`` minutes before each bar close.

    Bar ``i`` of the 5-minute series closes at ``ts[i]``, the instant the next
    window opens, so the minutes that count are the 1-minute bars closing in
    ``(ts[i] - 60 * window, ts[i]]``.  Pooled, not averaged: the share is the
    summed taker volume over the summed total volume, so a busy minute weighs
    more than a quiet one.  NaN unless every minute in the span is present with
    a finite taker figure -- a missing minute would silently shrink the window,
    and a share over nothing is not a share.
    """
    ts = np.asarray(ts, dtype=np.int64)
    out = np.full(len(ts), np.nan)
    if window < 1 or minute is None or len(minute) == 0 or minute.taker_buy is None:
        return out
    mts = minute.ts
    taker = np.asarray(minute.taker_buy, dtype=float)
    finite = np.isfinite(taker)
    cum_taker = np.concatenate(([0.0], np.cumsum(np.where(finite, taker, 0.0))))
    cum_volume = np.concatenate(([0.0], np.cumsum(minute.volume)))
    cum_finite = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
    hi = np.searchsorted(mts, ts, side="right")
    lo = np.searchsorted(mts, ts - 60 * window, side="right")
    present = (hi - lo == window) & (cum_finite[hi] - cum_finite[lo] == window)
    volume = cum_volume[hi] - cum_volume[lo]
    ok = present & (volume > 0)
    out[ok] = (cum_taker[hi] - cum_taker[lo])[ok] / volume[ok]
    return out


def build_features(series: BarSeries, cfg: StrategyConfig,
                   reference: BarSeries | None = None,
                   minute: BarSeries | None = None) -> FeatureSet:
    """Compute the factor set for ``series`` under ``cfg``.

    ``reference`` is an optional correlated series (ETH by default) used only by
    the cross-asset gate; it is matched to the BTC bars by timestamp.  ``minute``
    is an optional 1-minute series of the same symbol, read only when
    ``gates.taker_flow.source`` is ``"1m"``.
    """
    g = cfg.gates
    o, h, l, c, v = series.open, series.high, series.low, series.close, series.volume
    n = len(series)
    f: dict[str, np.ndarray] = {}

    # -- data integrity ----------------------------------------------------- #
    gap = np.ones(n)
    if n > 1:
        gap[1:] = np.diff(series.ts) / float(BAR_SECONDS)
    f["gap_bars"] = gap

    same = np.zeros(n)
    if n > 1:
        same[1:] = (np.diff(c) == 0.0).astype(float)
    stale = np.zeros(n)
    run = 0.0
    for i in range(n):
        run = run + 1.0 if same[i] else 0.0
        stale[i] = run
    f["stale_bars"] = stale
    f["volume"] = v
    f["spread_bps"] = (series.spread_bps if series.spread_bps is not None
                       else np.zeros(n))

    # -- volatility --------------------------------------------------------- #
    atr = ind.atr(h, l, c, g.volatility_regime.atr_period)
    f["atr"] = atr
    with np.errstate(invalid="ignore", divide="ignore"):
        f["atr_bps"] = np.where(c > 0, atr / c * 10_000.0, np.nan)
    f["atr_rank"] = ind.rolling_rank(np.nan_to_num(atr, nan=0.0),
                                     g.volatility_regime.rank_window)
    bbw = ind.bollinger_width(c, g.volatility_regime.bb_period)
    f["bb_width"] = bbw
    f["bb_width_rank"] = ind.rolling_rank(np.nan_to_num(bbw, nan=0.0),
                                          g.volatility_regime.rank_window)

    # -- trend -------------------------------------------------------------- #
    t = g.trend_alignment
    f["ema_fast"] = ind.ema(c, t.fast)
    f["ema_mid"] = ind.ema(c, t.mid)
    f["ema_slow"] = ind.ema(c, t.slow)
    slope = ind.linreg_slope(c, t.r2_window)
    f["trend_slope"] = slope
    f["trend_r2"] = ind.linreg_r2(c, t.r2_window)
    with np.errstate(invalid="ignore", divide="ignore"):
        f["trend_slope_atr"] = np.where(atr > 0, slope / atr, np.nan)

    idx, _, _, _, _, htf_close, _ = ind.resample_ohlcv(
        series.ts, o, h, l, c, v, t.htf_factor)
    htf_ema = ind.ema(htf_close, t.htf_ema) if len(htf_close) else np.array([])
    htf_slope = (_slope(htf_ema, t.htf_slope_lookback) if len(htf_ema)
                 else np.array([]))
    f["htf_ema"] = _htf_to_base(htf_ema, idx, n)
    f["htf_ema_slope"] = _htf_to_base(htf_slope, idx, n)
    f["htf_close"] = _htf_to_base(htf_close, idx, n)

    vwap = ind.rolling_vwap(h, l, c, v, t.vwap_window)
    f["vwap"] = vwap
    with np.errstate(invalid="ignore", divide="ignore"):
        f["vwap_dist_atr"] = np.where(atr > 0, (c - vwap) / atr, np.nan)

    # -- momentum ----------------------------------------------------------- #
    m = g.momentum_thrust
    roc = ind.roc(c, m.roc_bars)
    f["roc"] = roc
    f["roc_z"] = ind.zscore(np.nan_to_num(roc, nan=0.0), m.roc_z_window)
    f["rsi"] = ind.rsi(c, m.rsi_period)
    macd_line, macd_sig, macd_hist = ind.macd(c)
    f["macd"] = macd_line
    f["macd_signal"] = macd_sig
    f["macd_hist"] = macd_hist
    f["macd_hist_prev"] = _shift(macd_hist, 1)
    f["body_dominance"] = ind.body_dominance(o, h, l, c)

    # -- participation ------------------------------------------------------ #
    p = g.participation
    vol_med = ind.rolling_median(v, p.volume_window)
    f["volume_median"] = vol_med
    with np.errstate(invalid="ignore", divide="ignore"):
        f["volume_ratio"] = np.where(vol_med > 0, v / vol_med, np.nan)
    obv = ind.obv(c, v)
    f["obv"] = obv
    obv_slope = ind.linreg_slope(obv, p.obv_slope_lookback)
    with np.errstate(invalid="ignore", divide="ignore"):
        f["obv_slope_norm"] = np.where(vol_med > 0, obv_slope / vol_med, np.nan)

    # -- location ----------------------------------------------------------- #
    loc = g.location
    prior_high = _shift(ind.rolling_max(h, loc.swing_lookback), 1)
    prior_low = _shift(ind.rolling_min(l, loc.swing_lookback), 1)
    f["swing_high"] = prior_high
    f["swing_low"] = prior_low
    with np.errstate(invalid="ignore", divide="ignore"):
        f["room_up_atr"] = np.where(atr > 0, (prior_high - c) / atr, np.nan)
        f["room_down_atr"] = np.where(atr > 0, (c - prior_low) / atr, np.nan)

    # -- persistence / regime ---------------------------------------------- #
    pers = g.persistence
    f["variance_ratio"] = ind.variance_ratio(c, pers.vr_window, pers.vr_q)
    adx, plus_di, minus_di = ind.adx(h, l, c, pers.adx_period)
    f["adx"] = adx
    f["plus_di"] = plus_di
    f["minus_di"] = minus_di

    # -- mean reversion ----------------------------------------------------- #
    mr = g.mean_reversion
    f["close_z"] = ind.zscore(c, mr.z_window)
    f["rsi_mr"] = ind.rsi(c, mr.rsi_period)

    # -- taker flow --------------------------------------------------------- #
    # The one input here that is not a function of price.  Absent from
    # synthetic bars and from CSVs written before the column existed, in which
    # case the features are NaN and the gate reports itself as warming up
    # rather than voting on nothing.
    tf = g.taker_flow
    if tf.source == "1m":
        share = (minute_taker_share(series.ts, minute, tf.window)
                 if minute is not None else np.full(n, np.nan))
    elif series.taker_buy is not None:
        with np.errstate(invalid="ignore", divide="ignore"):
            raw = np.where(v > 0, series.taker_buy / v, np.nan)
        share = raw if tf.window <= 1 else ind.sma(raw, tf.window)
    else:
        share = np.full(n, np.nan)
    f["taker_ratio"] = share
    f["taker_z"] = (ind.zscore(share, tf.z_window) if np.isfinite(share).any()
                    else np.full(n, np.nan))

    # -- clock -------------------------------------------------------------- #
    f["hour_utc"] = ((series.ts // 3600) % 24).astype(float)
    f["minute_of_hour"] = ((series.ts % 3600) // 60).astype(float)
    f["weekday"] = (((series.ts // 86_400) + 4) % 7).astype(float)  # 0=Mon

    # -- cross asset -------------------------------------------------------- #
    ref_roc = np.full(n, np.nan)
    if reference is not None and len(reference) > g.cross_asset.roc_bars:
        rroc = ind.roc(reference.close, g.cross_asset.roc_bars)
        pos = np.searchsorted(reference.ts, series.ts, side="right") - 1
        ok = pos >= 0
        ref_roc[ok] = rroc[pos[ok]]
    f["ref_roc"] = ref_roc

    return FeatureSet(series=series, values=f)
