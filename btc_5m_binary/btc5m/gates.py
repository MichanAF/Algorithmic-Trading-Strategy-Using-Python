"""The gate menu.

A gate answers one narrow question about the next five minutes and returns a
verdict plus the sub-checks behind it.  Three kinds exist:

* **veto gates** (``kind = "veto"``) have no directional opinion.  They decide
  whether the market is fit to bet on at all.  One failure kills the bet.
* **directional gates** (``kind = "directional"``) vote UP or DOWN and carry a
  0..1 score.  The betting rules require them to agree before a stake is placed.
* **confirmation gates** (``kind = "confirm"``) run last, are told the side the
  directional gates chose, and may only veto it.  "Do not buy into resistance"
  is a veto on a long, not a reason to go short -- a gate that votes for
  whichever side has more room fights the trend gates and costs money.

Every gate records its own sub-checks, so a rejected bar can always be traced
to the exact condition that stopped it rather than to "the model said no".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .config import GateParams
from .features import FeatureSet

UP, DOWN, FLAT = 1, -1, 0


def direction_name(d: int) -> str:
    return {UP: "UP", DOWN: "DOWN", FLAT: "FLAT"}[int(d)]


@dataclass(frozen=True)
class Check:
    """One atomic condition inside a gate or a betting/risk rule.

    ``applicable`` marks a condition that could not be judged because an earlier
    one already settled the bar -- there is no point reporting a missing edge
    when no side was ever proposed.  Inapplicable checks are shown in the trace
    but never counted as the reason a bet was refused.
    """

    label: str
    passed: bool
    detail: str
    applicable: bool = True

    def __str__(self) -> str:
        state = "PASS" if self.passed else "FAIL"
        if not self.applicable:
            state = "SKIP"
        return f"{state} {self.label}: {self.detail}"


@dataclass(frozen=True)
class GateResult:
    name: str
    kind: str
    passed: bool
    direction: int
    score: float
    checks: tuple[Check, ...] = ()
    note: str = ""

    @property
    def is_veto(self) -> bool:
        return self.kind == "veto"

    @property
    def is_confirm(self) -> bool:
        return self.kind == "confirm"

    @property
    def is_directional(self) -> bool:
        return self.kind == "directional"

    @property
    def failed_checks(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def summary(self) -> str:
        head = f"{self.name:18s} {'PASS' if self.passed else 'FAIL'}"
        if self.kind == "veto":
            head += "  (veto)"
        elif self.kind == "confirm":
            head += f"  (confirm {direction_name(self.direction):4s}) score={self.score:.2f}"
        else:
            head += f"  {direction_name(self.direction):4s} score={self.score:.2f}"
        if self.note:
            head += f"  -- {self.note}"
        return head


def _saturate(x: float, low: float, high: float) -> float:
    """Map x from [low, high] onto [0, 1], clipped outside."""
    if high <= low:
        return 1.0 if x >= high else 0.0
    return float(min(1.0, max(0.0, (x - low) / (high - low))))


class Gate:
    """Base class.  Subclasses implement ``_evaluate`` and declare their inputs."""

    name: str = "gate"
    kind: str = "directional"          # "veto" | "directional" | "confirm"
    requires: tuple[str, ...] = ()
    params_key: str = ""

    @property
    def is_veto(self) -> bool:
        return self.kind == "veto"

    @property
    def is_confirm(self) -> bool:
        return self.kind == "confirm"

    @property
    def is_directional(self) -> bool:
        return self.kind == "directional"

    def params(self, gp: GateParams):
        return getattr(gp, self.params_key)

    def evaluate(self, fs: FeatureSet, i: int, gp: GateParams,
                 proposed: int = FLAT) -> GateResult:
        if self.is_confirm and proposed == FLAT:
            return self._result(False, FLAT, 0.0,
                                (Check("has_proposed_side", False,
                                       "no direction proposed to confirm"),),
                                note="nothing to confirm")
        if not fs.ready(i, self.requires):
            missing = [n for n in self.requires if not np.isfinite(fs.values[n][i])]
            return GateResult(self.name, self.kind, False, FLAT, 0.0,
                              (Check("warmup", False, f"not available: {missing}"),),
                              note="warming up")
        if self.is_confirm:
            return self._confirm(fs, i, self.params(gp), proposed)
        return self._evaluate(fs, i, self.params(gp))

    def _evaluate(self, fs: FeatureSet, i: int, p) -> GateResult:  # pragma: no cover
        raise NotImplementedError

    def _confirm(self, fs: FeatureSet, i: int, p,
                 proposed: int) -> GateResult:  # pragma: no cover
        raise NotImplementedError

    def _result(self, passed: bool, direction: int, score: float,
                checks: Sequence[Check], note: str = "") -> GateResult:
        return GateResult(self.name, self.kind, passed, direction,
                          float(min(1.0, max(0.0, score))), tuple(checks), note)


# --------------------------------------------------------------------------- #
# veto gates
# --------------------------------------------------------------------------- #


class DataIntegrityGate(Gate):
    """Veto -- is the feed trustworthy on this bar?

    A missed bar, a stuck price or a blown-out spread each turn a 5-minute
    binary into a coin flip with worse odds.  Cheapest gate in the stack and
    the one that saves the most money.
    """

    name = "data_integrity"
    kind = "veto"
    params_key = "data_integrity"
    requires = ("gap_bars", "stale_bars", "volume", "spread_bps")

    def _evaluate(self, fs, i, p):
        gap = fs.get("gap_bars", i)
        stale = fs.get("stale_bars", i)
        vol = fs.get("volume", i)
        spread = fs.get("spread_bps", i)
        checks = [
            Check("no_bar_gap", gap <= p.max_gap_bars,
                  f"spacing {gap:.2f} bars <= {p.max_gap_bars}"),
            Check("feed_not_stale", stale < p.max_stale_bars,
                  f"{stale:.0f} flat closes < {p.max_stale_bars}"),
            Check("spread_ok", spread <= p.max_spread_bps,
                  f"{spread:.2f} bps <= {p.max_spread_bps}"),
        ]
        if p.require_positive_volume:
            checks.append(Check("has_volume", vol > 0.0, f"volume {vol:.4f} > 0"))
        ok = all(c.passed for c in checks)
        return self._result(ok, FLAT, 1.0 if ok else 0.0, checks)


class VolatilityRegimeGate(Gate):
    """Veto -- is there enough movement to pay for the spread, but not chaos?

    The floor assumes you pay a cost proportional to being barely right: on spot
    or perps, a move smaller than the spread you cross loses money even when the
    direction is correct.

    That assumption does NOT hold for a yes/no contract, and this is the gate to
    drop there.  On a binary market any non-zero move resolves the bet and you
    pay a fixed contract price, not a spread.  Measured over 1,042 unseen days,
    accuracy is flat across the whole volatility range -- 57.9% below the floor,
    58.2% inside the band, 57.1% above the ceiling, against standard errors near
    2.5% -- so on a contract market the band only discards signals.  Opening it
    up doubled signal frequency (3.8 to 8.0 a day) and slightly *raised*
    accuracy.  See configs/trustwallet-bnb-5m.json, which leaves this gate out.

    Extreme volatility is still worth standing down for, but that is a tail
    guard and belongs in the risk layer, as ``risk.atr_shock_rank``.
    """

    name = "volatility_regime"
    kind = "veto"
    params_key = "volatility_regime"
    requires = ("atr_rank", "atr_bps", "bb_width_rank")

    def _evaluate(self, fs, i, p):
        rank = fs.get("atr_rank", i)
        atr_bps = fs.get("atr_bps", i)
        bbw_rank = fs.get("bb_width_rank", i)
        checks = [
            Check("atr_above_floor", rank >= p.min_rank,
                  f"ATR pct-rank {rank:.2f} >= {p.min_rank}"),
            Check("atr_below_ceiling", rank <= p.max_rank,
                  f"ATR pct-rank {rank:.2f} <= {p.max_rank}"),
            Check("move_covers_cost", atr_bps >= p.min_atr_bps,
                  f"ATR {atr_bps:.1f} bps >= {p.min_atr_bps}"),
            Check("not_expansion_shock", bbw_rank <= p.max_bb_width_rank,
                  f"BB-width rank {bbw_rank:.2f} <= {p.max_bb_width_rank}"),
        ]
        ok = all(c.passed for c in checks)
        centre = (p.min_rank + p.max_rank) / 2.0
        half = max(1e-9, (p.max_rank - p.min_rank) / 2.0)
        score = max(0.0, 1.0 - abs(rank - centre) / half) if ok else 0.0
        return self._result(ok, FLAT, score, checks,
                            note=f"ATR {atr_bps:.1f} bps, rank {rank:.2f}")


class SessionGate(Gate):
    """Veto, optional -- clock-based exclusions.

    Perp funding settles on the eight-hour clock and the minutes around it are
    mechanical flow, not information.  Thin hours widen spreads without
    widening the move.
    """

    name = "session"
    kind = "veto"
    params_key = "session"
    requires = ("hour_utc", "minute_of_hour", "weekday")

    def _evaluate(self, fs, i, p):
        hour = int(fs.get("hour_utc", i))
        minute = fs.get("minute_of_hour", i)
        weekday = int(fs.get("weekday", i))
        near_funding = any(
            (hour == fh and minute < p.funding_blackout_minutes)
            or (hour == (fh - 1) % 24 and minute >= 60 - p.funding_blackout_minutes)
            for fh in p.funding_hours_utc
        )
        checks = [
            Check("hour_allowed", hour in p.allowed_hours_utc,
                  f"{hour:02d}:00 UTC in allowed hours"),
            Check("outside_funding_window", not near_funding,
                  f"{hour:02d}:{minute:02.0f} vs funding {p.funding_hours_utc} "
                  f"+/-{p.funding_blackout_minutes}m"),
        ]
        if p.skip_weekend:
            checks.append(Check("weekday_only", weekday < 5,
                                f"weekday index {weekday} < 5"))
        ok = all(c.passed for c in checks)
        return self._result(ok, FLAT, 1.0 if ok else 0.0, checks)


# --------------------------------------------------------------------------- #
# directional gates
# --------------------------------------------------------------------------- #


class TrendAlignmentGate(Gate):
    """Directional -- which way, and does the 15-minute chart agree?

    The EMA stack picks the side; the higher timeframe has to nod.  Requiring
    agreement across timeframes is what stops the stack from buying a 5-minute
    bounce inside a 15-minute downtrend.
    """

    name = "trend_alignment"
    params_key = "trend_alignment"
    requires = ("ema_fast", "ema_mid", "ema_slow", "htf_ema_slope",
                "trend_r2", "trend_slope_atr", "vwap", "close_z")

    def _evaluate(self, fs, i, p):
        fast, mid, slow = (fs.get("ema_fast", i), fs.get("ema_mid", i),
                           fs.get("ema_slow", i))
        htf_slope = fs.get("htf_ema_slope", i)
        r2 = fs.get("trend_r2", i)
        slope_atr = fs.get("trend_slope_atr", i)
        price = float(fs.series.close[i])
        vwap = fs.get("vwap", i)

        if fast > mid > slow:
            direction = UP
        elif fast < mid < slow:
            direction = DOWN
        else:
            direction = FLAT

        checks = [
            Check("ema_stack_aligned", direction != FLAT,
                  f"EMA {fast:.1f}/{mid:.1f}/{slow:.1f} -> "
                  f"{direction_name(direction)}"),
            Check("htf_agrees", direction != FLAT and np.sign(htf_slope) == direction,
                  f"15m EMA slope {htf_slope:+.2f} vs {direction_name(direction)}"),
            Check("move_is_orderly", r2 >= p.min_r2,
                  f"regression R2 {r2:.2f} >= {p.min_r2}"),
            Check("slope_meaningful",
                  direction != FLAT
                  and np.sign(slope_atr) == direction
                  and abs(slope_atr) >= p.min_slope_atr_frac,
                  f"slope {slope_atr:+.3f} ATR/bar vs "
                  f"+/-{p.min_slope_atr_frac}"),
        ]
        if p.require_vwap_side:
            on_side = (price > vwap) if direction == UP else (price < vwap)
            checks.append(Check("vwap_side", direction != FLAT and on_side,
                                f"price {price:.1f} vs VWAP {vwap:.1f}"))

        ok = all(c.passed for c in checks)
        score = 0.5 * _saturate(r2, p.min_r2, 0.75) + 0.5 * _saturate(
            abs(slope_atr), p.min_slope_atr_frac, p.min_slope_atr_frac * 5.0)
        return self._result(ok, direction if ok else FLAT, score if ok else 0.0,
                            checks, note=f"R2 {r2:.2f}, slope {slope_atr:+.3f} ATR")


class MomentumThrustGate(Gate):
    """Directional -- is it moving right now, with something left in the tank?

    The rate-of-change z-score says the current push is unusual for this market.
    The RSI ceiling is the other half: a thrust that has already run to an
    extreme is the tail of a move, and the next five minutes belong to the fade.
    """

    name = "momentum_thrust"
    params_key = "momentum_thrust"
    requires = ("roc_z", "rsi", "macd_hist", "macd_hist_prev", "body_dominance")

    def _evaluate(self, fs, i, p):
        z = fs.get("roc_z", i)
        rsi = fs.get("rsi", i)
        hist = fs.get("macd_hist", i)
        hist_prev = fs.get("macd_hist_prev", i)
        body = fs.get("body_dominance", i)
        direction = UP if z > 0 else DOWN if z < 0 else FLAT

        lo, hi = ((p.rsi_long_min, p.rsi_long_max) if direction == UP
                  else (p.rsi_short_min, p.rsi_short_max))
        checks = [
            Check("thrust_is_unusual", abs(z) >= p.min_roc_z,
                  f"{p.roc_bars}-bar ROC z {z:+.2f} vs +/-{p.min_roc_z}"),
            Check("rsi_in_band", direction != FLAT and lo <= rsi <= hi,
                  f"RSI {rsi:.1f} in [{lo}, {hi}] for "
                  f"{direction_name(direction)}"),
            Check("candle_is_decisive",
                  direction != FLAT
                  and np.sign(body) == direction
                  and abs(body) >= p.min_body_dominance,
                  f"body {body:+.2f} of range vs "
                  f"+/-{p.min_body_dominance}"),
        ]
        if p.require_macd_expansion:
            expanding = (direction != FLAT
                         and np.sign(hist) == direction
                         and abs(hist) > abs(hist_prev))
            checks.append(Check("macd_expanding", expanding,
                                f"hist {hist:+.2f} vs prev {hist_prev:+.2f}"))

        ok = all(c.passed for c in checks)
        headroom = (_saturate(hi - rsi, 0.0, 12.0) if direction == UP
                    else _saturate(rsi - lo, 0.0, 12.0))
        score = 0.65 * _saturate(abs(z), p.min_roc_z, p.min_roc_z + 2.0) + 0.35 * headroom
        return self._result(ok, direction if ok else FLAT, score if ok else 0.0,
                            checks, note=f"ROC z {z:+.2f}, RSI {rsi:.1f}")


class ParticipationGate(Gate):
    """Directional -- is real volume behind the move?

    A drift on thin volume reverses as soon as one real order arrives.  The
    upper bound matters just as much: a bar at six times median volume is a
    blow-off, and blow-offs are where continuation bets go to die.

    Overlap note, and it is not flattering: on the bundled fixture this gate's
    verdict adds no information once the other gates have agreed (separation
    +1.5 points, z = +1.9, under the bar).  It removes about 80% of the
    remaining signals to buy roughly 1.5 points of accuracy, which is a losing
    trade on a risk-adjusted basis -- see the `trend4` preset and README
    "Overlap analysis".  Kept in the menu because volume confirmation may well
    earn its place on real data with real order flow; measure it with
    `btc5m overlap` before trusting it.
    """

    name = "participation"
    params_key = "participation"
    requires = ("volume_ratio", "obv_slope_norm", "body_dominance")

    def _evaluate(self, fs, i, p):
        ratio = fs.get("volume_ratio", i)
        obv_slope = fs.get("obv_slope_norm", i)
        body = fs.get("body_dominance", i)
        direction = (UP if obv_slope > 0 else DOWN if obv_slope < 0
                     else (UP if body > 0 else DOWN if body < 0 else FLAT))
        checks = [
            Check("volume_confirms", ratio >= p.min_volume_ratio,
                  f"bar volume {ratio:.2f}x median >= {p.min_volume_ratio}"),
            Check("not_a_blowoff", ratio <= p.max_volume_ratio,
                  f"bar volume {ratio:.2f}x median <= {p.max_volume_ratio}"),
            Check("flow_has_direction", direction != FLAT,
                  f"OBV slope {obv_slope:+.3f} -> {direction_name(direction)}"),
        ]
        if p.require_obv_agreement:
            checks.append(Check("obv_agrees_with_candle",
                                direction != FLAT and np.sign(body) == direction,
                                f"body {body:+.2f} vs flow "
                                f"{direction_name(direction)}"))
        ok = all(c.passed for c in checks)
        score = _saturate(ratio, p.min_volume_ratio, p.min_volume_ratio + 1.2)
        return self._result(ok, direction if ok else FLAT, score if ok else 0.0,
                            checks, note=f"{ratio:.2f}x median volume")


class LocationGate(Gate):
    """Confirmation -- is there room to run on the proposed side, and are we chasing?

    Checks the side the directional gates already chose: enough space before the
    recent swing level, and not already stretched from VWAP.  It is a veto on
    that side, never a vote for the other one -- the version that voted for
    whichever side had more room tested at 44% accuracy, because "more room
    below" is a mean-reversion opinion picking a fight with the trend gates.
    """

    name = "location"
    kind = "confirm"
    params_key = "location"
    requires = ("room_up_atr", "room_down_atr", "vwap_dist_atr")

    def _confirm(self, fs, i, p, proposed):
        up_room = fs.get("room_up_atr", i)
        down_room = fs.get("room_down_atr", i)
        extension = fs.get("vwap_dist_atr", i)
        room = up_room if proposed == UP else down_room
        checks = [
            Check("room_to_swing", room >= p.min_room_atr,
                  f"{room:.2f} ATR to the {direction_name(proposed)} swing "
                  f">= {p.min_room_atr}"),
            Check("not_chasing", abs(extension) <= p.max_extension_atr,
                  f"{abs(extension):.2f} ATR from VWAP <= {p.max_extension_atr}"),
        ]
        ok = all(c.passed for c in checks)
        score = _saturate(room, p.min_room_atr, p.min_room_atr * 3.0)
        return self._result(ok, proposed if ok else FLAT, score if ok else 0.0,
                            checks,
                            note=f"room up {up_room:.2f} / down {down_room:.2f} ATR")


class PersistenceGate(Gate):
    """Directional -- does this regime continue moves or reverse them?

    The variance ratio is the honest test of whether momentum is the right tool
    right now.  Above 1, five-minute moves extend.  Below 1, following them is
    paying the spread to be wrong.

    Overlap note: its +DI/-DI direction agrees with ``trend_alignment`` on about
    99% of the bars where both pass, so this gate is not a second independent
    opinion on *which way*.  What it adds is the regime test -- the variance
    ratio and ADX floors -- and that does carry information the trend gate does
    not (measured separation +4.8 points, z = +5.2).  Read a stack containing
    both as two direction votes and one regime filter, not three votes.
    """

    name = "persistence"
    params_key = "persistence"
    requires = ("variance_ratio", "adx", "plus_di", "minus_di")

    def _evaluate(self, fs, i, p):
        vr = fs.get("variance_ratio", i)
        adx = fs.get("adx", i)
        plus_di, minus_di = fs.get("plus_di", i), fs.get("minus_di", i)
        direction = UP if plus_di > minus_di else DOWN if minus_di > plus_di else FLAT
        checks = [
            Check("returns_trend", vr >= p.min_variance_ratio,
                  f"variance ratio {vr:.2f} >= {p.min_variance_ratio}"),
            Check("directional_strength", adx >= p.min_adx,
                  f"ADX {adx:.1f} >= {p.min_adx}"),
            Check("di_has_direction", direction != FLAT,
                  f"+DI {plus_di:.1f} vs -DI {minus_di:.1f}"),
        ]
        ok = all(c.passed for c in checks)
        score = 0.5 * _saturate(vr, p.min_variance_ratio, p.min_variance_ratio + 0.6) \
            + 0.5 * _saturate(adx, p.min_adx, p.min_adx + 20.0)
        return self._result(ok, direction if ok else FLAT, score if ok else 0.0,
                            checks, note=f"VR {vr:.2f}, ADX {adx:.1f}")


class MeanReversionGate(Gate):
    """Directional, alternative personality -- fade a stretched, exhausted move.

    Swap this in for ``trend_alignment`` and the stack stops following moves and
    starts betting against them.  Requires a mean-reverting variance ratio, so
    the two gates are mutually exclusive by construction, not by convention.
    """

    name = "mean_reversion"
    params_key = "mean_reversion"
    requires = ("close_z", "volume_ratio", "rsi_mr", "variance_ratio")

    def _evaluate(self, fs, i, p):
        z = fs.get("close_z", i)
        ratio = fs.get("volume_ratio", i)
        rsi = fs.get("rsi_mr", i)
        vr = fs.get("variance_ratio", i)
        direction = DOWN if z > 0 else UP if z < 0 else FLAT  # fade the stretch
        rsi_extreme = (rsi >= p.rsi_overbought if direction == DOWN
                       else rsi <= p.rsi_oversold)
        checks = [
            Check("price_stretched", abs(z) >= p.min_abs_z,
                  f"close z {z:+.2f} vs +/-{p.min_abs_z}"),
            Check("rsi_exhausted", direction != FLAT and rsi_extreme,
                  f"RSI {rsi:.1f} vs "
                  f"{p.rsi_overbought}/{p.rsi_oversold}"),
            Check("exhaustion_volume", ratio >= p.exhaustion_volume_ratio,
                  f"volume {ratio:.2f}x median >= {p.exhaustion_volume_ratio}"),
            Check("regime_mean_reverts", vr <= p.max_variance_ratio,
                  f"variance ratio {vr:.2f} <= {p.max_variance_ratio}"),
        ]
        ok = all(c.passed for c in checks)
        score = 0.6 * _saturate(abs(z), p.min_abs_z, p.min_abs_z + 1.5) \
            + 0.4 * _saturate(ratio, p.exhaustion_volume_ratio,
                              p.exhaustion_volume_ratio + 1.5)
        return self._result(ok, direction if ok else FLAT, score if ok else 0.0,
                            checks, note=f"z {z:+.2f}, RSI {rsi:.1f}, VR {vr:.2f}")


class CrossAssetGate(Gate):
    """Confirmation -- require a correlated asset to be moving the same way.

    ETH usually leads BTC on impulsive five-minute moves.  When the two
    disagree, one of them is noise and you do not know which, so this confirms
    the proposed side rather than proposing one of its own.
    """

    name = "cross_asset"
    kind = "confirm"
    params_key = "cross_asset"
    requires = ("ref_roc",)

    def evaluate(self, fs, i, gp, proposed=FLAT):
        if not np.isfinite(fs.values["ref_roc"]).any():
            return self._result(
                False, FLAT, 0.0,
                (Check("reference_available", False,
                       "no reference series supplied -- pass --reference ETH.csv"),),
                note="no reference data")
        return super().evaluate(fs, i, gp, proposed)

    def _confirm(self, fs, i, p, proposed):
        ref = fs.get("ref_roc", i)
        bps = abs(ref) * 10_000.0
        direction = UP if ref > 0 else DOWN if ref < 0 else FLAT
        checks = [
            Check("reference_is_moving", bps >= p.min_abs_roc_bps,
                  f"reference {p.roc_bars}-bar ROC {bps:.1f} bps "
                  f">= {p.min_abs_roc_bps}"),
            Check("reference_agrees", direction == proposed or not p.require_same_direction,
                  f"reference ROC {ref * 10_000:+.1f} bps vs "
                  f"{direction_name(proposed)}"),
        ]
        ok = all(c.passed for c in checks)
        score = _saturate(bps, p.min_abs_roc_bps, p.min_abs_roc_bps * 4.0)
        return self._result(ok, proposed if ok else FLAT, score if ok else 0.0,
                            checks, note=f"reference {ref * 10_000:+.1f} bps")


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

GATE_REGISTRY: dict[str, Gate] = {
    g.name: g for g in (
        DataIntegrityGate(),
        VolatilityRegimeGate(),
        SessionGate(),
        TrendAlignmentGate(),
        MomentumThrustGate(),
        ParticipationGate(),
        LocationGate(),
        PersistenceGate(),
        MeanReversionGate(),
        CrossAssetGate(),
    )
}


_KIND_ORDER = {"veto": 0, "directional": 1, "confirm": 2}


def build_stack(names: Sequence[str]) -> list[Gate]:
    """Resolve gate names to gates, ordered veto -> directional -> confirm.

    Vetoes run first so a rejected bar costs almost nothing, and confirmation
    gates run last because they need the side the directional gates chose.
    """
    unknown = [n for n in names if n not in GATE_REGISTRY]
    if unknown:
        raise ValueError(f"unknown gate(s) {unknown}; "
                         f"available: {sorted(GATE_REGISTRY)}")
    gates = [GATE_REGISTRY[n] for n in names]
    return sorted(gates, key=lambda g: _KIND_ORDER[g.kind])
