# Decision log: the fade + order-flow config

The protocol in one line: choose each value by reasoning on the development
year (the year ending 2024-09-13), write the choice here with the evidence it
was made on, and only when every value is written fetch the fresh holdout --
the year ending 2023-09-13, which nothing in this repository has read -- and
run the config on it once. This file is not edited after that run, except to
record the result.

The person building the strategy makes every choice. The numbers are what the
tooling measured, with the run each came from, so any of them can be re-read.
Nothing below was read from the holdout year ending 2025-09-13 (burned for
the fade config) or from the year ending 2023-09-13.

## Decision 1: what the second gate is -- A and B

- **Options.** A: keep the fade gate (`mean_reversion`), which passed its
  pre-registered holdout. B: add order flow (`taker_flow`), the one input in
  hand that is not a function of price. Every price-derived gate measured at
  or below break-even on real data.
- **Chosen:** A and B, together.
- **Evidence.** The gate table on the development year (run 34749740287):
  `mean_reversion` 54.73% (+2.73%, z +2.0); every other price gate negative
  and significant; `taker_flow` at its placeholders (follow) 47.06%, z -8.5,
  so its complement, fade, 52.94% on the same 7,369 signals.

## Decision 2: taker_flow mode, source and window -- fade, the bar's own share

- **Options.** `mode` follow or fade. `source` the 5-minute bar's own share
  (`5m`, `window` 1) or the last 1, 2 or 3 minutes before the open (`1m`).
- **Chosen:** `mode: fade`, `source: 5m`, `window: 1`.
- **Evidence** (run 34750893609, development year, 104,789 graded windows,
  noise band +/-0.0062). Correlation of the pre-open taker share with the
  window's direction: last 1 min -0.0099, last 2 min -0.0150, last 3 min
  -0.0171, full bar -0.0308. Negative in every cell, so the flow reverts and
  fade is the only mode with a positive sign; strongest over the whole bar,
  so the bar is the read. The 1-minute path stays as the instrument that
  measured this.
- **Not chosen, and why.** The last minute alone was the weakest of the five
  reads and cleared break-even at no |z| floor with enough signals to judge.

## Decisions 3 and 4: the |z| floor and the volume floor -- 2.0 and none

- **Chosen:** `min_abs_z: 2.0`, `min_volume_ratio: 0` (candidate C above).
- **Not chosen, and why.** Candidate A (|z| 1.0 with a 2x volume floor)
  scored the same edge as bets (+1.62% against +1.53%) with a deeper
  drawdown (12.65% against 9.24%, near the 15% halt) and one more value to
  carry. The person building the strategy chose C.

## Decision 5: z-score span -- one day

- **Chosen:** `z_window: 288`, unchanged from the placeholder. No evidence
  was read for or against it; it is the fade gate's own convention scaled
  to the taker share.

## Decision 6: how the gates combine -- either may fire

- **Chosen:** `betting.mode: weighted`, `allow_dissent: false`. Either gate
  alone can carry a bar; when both fire they must agree.
- **Not chosen, and why.** Unanimous (both must agree): 0.08 signals a day
  and no bets in a year for candidate C. A wiring that never fires cannot be
  tested on any holdout.

## Decision 7: how the taker vote is scored -- flat

- **Chosen:** `score_span: 0`. A passing vote is a full yes. `min_conviction`
  stays at the fade config's 0.85; every fade-gate value is unchanged, so the
  fade gate's own bets are exactly what they were.
- **Not chosen, and why.** The sloped vote (span 2.0, the placeholder)
  placed 320 bets a year at 52.19%: the conviction floor kept only the most
  extreme imbalances, which the grid in item 4 shows are not the better ones.

## The pre-registered run

- **Config:** `configs/fade-flow-5m.json`, written 2026-09-13 after the
  decisions above and before any fetch of the fresh holdout.
- **Fresh holdout:** the year ending 2023-09-13. Nothing in this repository
  had read it when the config was written.
- **Pass criterion, fixed before the fetch:** on the fresh holdout the
  backtest's hit rate beats the 52.00% break-even by at least +1.0%,
  significant at two standard errors, with no halt. Anything less fails.
- **Development-year reference** (run 34753701038): 4,632 bets, 53.53%,
  +1.53%, drawdown 9.24%, longest loss streak 9.
- **Result:** pending the first run of `fade-flow-hypothesis.yml`.
