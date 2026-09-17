"""Hourly price and funding, together.

Funding is the whole point of this strategy, so a price series on its own is
not enough to test anything. The two have to arrive as one object, on the same
clock -- Hyperliquid's hourly funding stamp -- or the backtest is measuring a
directional strategy and calling it carry.

``synthetic`` generates both with the correlation that makes the problem hard:
funding is rich *because* price has been rising, which means the carry signal
and the risk signal disagree most of the time and agree at the worst moments.
A generator that drew funding independently of price would make the strategy
look far better than it is.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HOUR = 3600


@dataclass
class Series:
    """Hourly bars: timestamp, price, and the funding rate paid that hour."""

    ts: np.ndarray
    price: np.ndarray
    funding: np.ndarray          # per-hour rate; positive = longs pay shorts
    symbol: str = "BTC"

    def __post_init__(self) -> None:
        n = len(self.price)
        if not (len(self.ts) == len(self.funding) == n):
            raise ValueError("ts, price and funding must be the same length")
        if n < 2:
            raise ValueError("need at least two bars")
        if np.any(self.price <= 0.0):
            raise ValueError("prices must be positive")

    def __len__(self) -> int:
        return len(self.price)

    @property
    def hours(self) -> int:
        return len(self.price)

    @property
    def days(self) -> float:
        return len(self.price) / 24.0

    def funding_apr(self) -> np.ndarray:
        return self.funding * 24.0 * 365.0

    def summary(self) -> str:
        apr = self.funding_apr()
        ret = self.price[-1] / self.price[0] - 1.0
        return (f"{self.symbol}: {self.hours:,} hours ({self.days:.0f} days), "
                f"price {self.price[0]:,.0f} -> {self.price[-1]:,.0f} ({ret:+.1%})\n"
                f"  funding: mean {apr.mean():+.2%} APR, median {np.median(apr):+.2%}, "
                f"negative {np.mean(apr < 0):.1%} of hours, "
                f"max {apr.max():+.1%}, min {apr.min():+.1%}")


def synthetic(hours: int = 24 * 365, seed: int = 7, start_price: float = 95_000.0,
              annual_vol: float = 0.55, annual_drift: float = 0.20,
              funding_beta: float = 0.55) -> Series:
    """Generate correlated price and funding.

    Funding is modelled as a mean-reverting process pulled toward the interest
    baseline, pushed by trailing price momentum, and occasionally spiked --
    which is roughly how perp funding behaves: quiet near baseline, rich in a
    rally, sharply negative in a flush.

    ``funding_beta`` is the coupling. Set it to zero and the carry signal
    becomes free money in the backtest, which is the tell that the coupling is
    doing real work.
    """
    if hours < 48:
        raise ValueError("need at least 48 hours")
    rng = np.random.default_rng(seed)

    dt = 1.0 / (24.0 * 365.0)
    vol_h = annual_vol * np.sqrt(dt)
    drift_h = (annual_drift - 0.5 * annual_vol ** 2) * dt

    # Stochastic volatility, so the vol-rank signal has something to rank.
    log_vol = np.zeros(hours)
    for i in range(1, hours):
        log_vol[i] = 0.995 * log_vol[i - 1] + 0.05 * rng.standard_normal()
    vol_path = vol_h * np.exp(log_vol - log_vol.var() / 2.0)

    shocks = rng.standard_normal(hours) * vol_path + drift_h
    price = start_price * np.exp(np.cumsum(shocks))

    # Trailing 7-day momentum, in vol units, drives the funding premium.
    window = 24 * 7
    momentum = np.zeros(hours)
    for i in range(hours):
        lo = max(0, i - window)
        past = price[lo:i + 1]
        if len(past) > 2:
            momentum[i] = (past[-1] / past[0] - 1.0) / (annual_vol * np.sqrt(len(past) / 8760.0))

    # Mean-reverting around a momentum-driven target.  kappa and sigma are set
    # so the stationary spread of funding is about 25% APR around a baseline of
    # 11% -- which puts roughly a quarter of hours negative, close to what perp
    # funding actually does outside a mania.  Turn sigma up and the carry signal
    # drowns; turn it down and the backtest flatters the strategy.
    baseline = 0.0000125
    kappa, sigma = 0.03, 0.0000055
    funding = np.zeros(hours)
    f = baseline
    for i in range(hours):
        target = baseline + funding_beta * baseline * 7.0 * np.tanh(momentum[i])
        f += kappa * (target - f) + sigma * rng.standard_normal()
        if rng.random() < 0.003:                      # occasional squeeze/flush
            f += rng.choice([-1.0, 1.0]) * abs(rng.normal(0.0, 0.00006))
        funding[i] = float(np.clip(f, -0.04, 0.04))   # venue cap

    ts = np.arange(hours, dtype=np.int64) * HOUR + 1_735_689_600
    return Series(ts=ts, price=price, funding=funding)


def load_csv(path: str | Path, symbol: str = "BTC") -> Series:
    """Load hourly ``timestamp,price,funding`` rows.

    ``funding`` is the per-hour rate as a decimal (0.0000125 = the baseline).
    A ``funding_apr`` column is accepted instead and converted, because that is
    how most dashboards display it and transcribing by hand is how sign errors
    get in.
    """
    rows = list(csv.DictReader(Path(path).open()))
    if not rows:
        raise ValueError(f"{path} has no rows")
    cols = {c.lower().strip(): c for c in rows[0]}

    def col(*names: str) -> str:
        for n in names:
            if n in cols:
                return cols[n]
        raise ValueError(f"{path} needs one of {names}; has {list(cols)}")

    ts_c = col("timestamp", "ts", "time", "open_time")
    px_c = col("price", "close", "mark", "oracle")
    ts = np.array([int(float(r[ts_c])) for r in rows], dtype=np.int64)
    if ts[0] > 10 ** 12:                               # milliseconds
        ts //= 1000
    price = np.array([float(r[px_c]) for r in rows], dtype=float)

    if "funding" in cols or "funding_rate" in cols:
        fc = cols.get("funding") or cols["funding_rate"]
        funding = np.array([float(r[fc]) for r in rows], dtype=float)
    else:
        fc = col("funding_apr", "apr")
        funding = np.array([float(r[fc]) for r in rows], dtype=float) / (24.0 * 365.0)

    order = np.argsort(ts)
    return Series(ts=ts[order], price=price[order], funding=funding[order],
                  symbol=symbol)
