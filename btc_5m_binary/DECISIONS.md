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
4. `min_volume_ratio` -- the volume floor, the bar's volume over its
   rolling median. Measured with the |z| floor on the bar's own share, faded
   (run 34752950985); the two values interact, so they are chosen together:

   | \|z\| >= | volume floor | signals/day | fade accuracy | vs 52.00% | z |
   |---|---|---|---|---|---|
   | 1.0 | none | 95.3 | 52.40% | +0.40% | +1.5 |
   | 1.0 | 1.0x | 47.7 | 52.71% | +0.71% | +1.9 |
   | 1.0 | 1.5x | 24.8 | 53.42% | +1.42% | +2.7 |
   | 1.0 | 2.0x | 14.8 | 54.01% | +2.01% | +3.0 |
   | 1.5 | none | 39.6 | 52.81% | +0.81% | +1.9 |
   | 1.5 | 1.0x | 20.2 | 52.94% | +0.94% | +1.6 |
   | 1.5 | 1.5x | 10.5 | 53.10% | +1.10% | +1.4 |
   | 1.5 | 2.0x | 6.2 | 53.47% | +1.47% | +1.4 |
   | 2.0 | none | 12.4 | 53.93% | +1.93% | +2.6 |
   | 2.0 | 1.0x | 6.5 | 53.51% | +1.51% | +1.5 |
   | 2.0 | 1.5x | 3.3 | 53.22% | +1.22% | +0.8 |
   | 2.0 | 2.0x | 1.9 | 54.08% | +2.08% | +1.1 |
   | 2.5 | any | <= 2.8 | 50.16% to 52.54% | negative to +0.54% | under +0.5 |

   Two readings. At a |z| floor of 1.0 the volume floor is monotone: every
   step up in required volume raises accuracy, to 54.01% at 2x median with
   14.8 signals a day. At a |z| floor of 2.0 the volume floor adds nothing
   the |z| floor had not already selected. Sixteen cells were read here on
   top of twenty before; the best cell's z is inflated by that selection,
   which is what the fresh holdout exists to correct.
5. `z_window` -- 288 bars (one day) unless there is a reason to change it.
6. How A and B combine. In unanimous mode every directional gate must
   pass, so a bar is tradable only when both fire and agree (AND); in
   weighted mode a silent gate casts no vote, so either gate alone can carry
   a bar and a disagreement is refused (OR). Measured for both taker
   candidates still open (run 34753309339, development year), signal level
   first, then as bets under the fade config's own betting and risk values:

   | candidate | cell | signals/day | accuracy | vs 52.00% | z |
   |---|---|---|---|---|---|
   | A (\|z\| 1.0, vol 2x) | mean_reversion alone | 2.71 | 54.35% | +2.35% | +1.5 |
   | A | taker_flow alone | 13.79 | 53.81% | +1.81% | +2.6 |
   | A | both fire, agree | 1.02 | 56.57% | +4.57% | +1.8 |
   | A | AND | 1.02 | 56.57% | +4.57% | +1.8 |
   | A | OR | 17.53 | 54.05% | +2.05% | +3.3 |
   | C (\|z\| 2.0, none) | mean_reversion alone | 3.71 | 54.86% | +2.86% | +2.1 |
   | C | taker_flow alone | 12.38 | 53.95% | +1.95% | +2.6 |
   | C | both fire, agree | 0.08 | 51.72% | -0.28% | 0.0 |
   | C | AND | 0.08 | too few | | |
   | C | OR | 16.16 | 54.15% | +2.15% | +3.3 |

   Bets, same run, fade config betting and risk: A unanimous 2 bets a year;
   A weighted 290 bets, 50.87%, -1.13%; C unanimous 0 bets; C weighted 320
   bets, 52.19%, +0.19%. The fade config alone on this year: 325 bets,
   52.31%. So AND cannot produce a bet stream, and OR as scored today turns
   16-17 signals a day into under one bet a day at break-even: the vote's
   score rises with |z| past the floor, the conviction floor of 0.85 then
   admits only the most extreme imbalances, and the grid in item 4 shows
   those are not the better ones. A flat vote (`score_span` 0: a pass is a
   full vote) is the candidate fix, measured next.

   Measured (run 34753701038, development year, fade config betting and
   risk, weighted wiring):

   | candidate | taker vote | bets/year | per day | hit rate | vs 52.00% | max drawdown | longest loss streak | Sharpe |
   |---|---|---|---|---|---|---|---|---|
   | A (\|z\| 1.0, vol 2x) | sloped (span 2.0) | 290 | 0.80 | 50.87% | -1.13% | 5.49% | 8 | -0.39 |
   | A | flat (span 0) | 4,787 | 13.15 | 53.62% +/- 0.72% | +1.62%, significant | 12.65% | 11 | 2.24 |
   | C (\|z\| 2.0, none) | sloped (span 2.0) | 320 | 0.88 | 52.19% | +0.19% | 4.28% | 7 | 0.07 |
   | C | flat (span 0) | 4,632 | 12.73 | 53.53% +/- 0.73% | +1.53%, significant | 9.24% | 9 | 2.09 |

   Unanimous wiring: 2 bets a year for A, none for C. The fade config alone
   on this year: 325 bets, 52.31%. Read with care: this is the development
   year, every value was chosen on it, and the backtest subtracts no gas --
   thirteen bets a day at $0.30 is about $1,400 a year, and at a $12 stake
   a +1.6% edge is worth about $0.37 a bet, so stake size (or a venue
   without gas) is where the economics stand or fall. A's drawdown sits
   close to the 15% halt.
7. The betting floor, `min_conviction`, which sets how far above `min_abs_z`
   a signal must be before it is a bet, and with it how the taker vote is
   scored (`score_span`). The flat vote leaves `min_conviction` at the fade
   config's 0.85 and the fade gate's own bets exactly as they were; only
   the taker vote changes from a slope to a yes.

Then: the config file, the workflow with the fresh holdout, one run.
