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

## Pending, in order

3. `min_abs_z` -- the |z| floor. Evidence on the bar's own share, faded, no
   volume floor (run 34750893609): |z| >= 1.0: 34,786 signals, 52.40%, z +1.5;
   >= 1.5: 14,439, 52.81%, z +1.9; >= 2.0: 4,538, 53.93%, z +2.6;
   >= 2.5: 1,006, 52.54%, z +0.3.
4. `min_volume_ratio` -- the volume floor. Measured next by the same
   workflow on the bar's own share at floors 0, 1, 1.5 and 2x median.
5. `z_window` -- 288 bars (one day) unless there is a reason to change it.
6. How A and B combine: both must agree, or either may fire; and
   `min_directional_gates`.
7. The betting floor, `min_conviction`, which sets how far above `min_abs_z`
   a signal must be before it is a bet.

Then: the config file, the workflow with the fresh holdout, one run.
