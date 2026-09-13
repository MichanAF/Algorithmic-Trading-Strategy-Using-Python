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
- **Result** ([run 34755099078](https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python/actions/runs/34755099078),
  the first and only run): **FAIL**, by the letter of the criterion. On the
  fresh holdout the config placed 4,663 bets at 54.31% +/- 0.73%, +2.31%
  over break-even, significant at two standard errors -- and the backtest
  halted: the drawdown reached 15.07% against the 15.00% limit, late in the
  year (320 signals were refused after the halt), with the year's P&L at
  +62.58% on the 5,000 bankroll before it. The edge and significance
  clauses passed by a wide margin; the no-halt clause failed by 0.07 of a
  point.

  Four years, one config, nothing changed between them:

  | year | bets | hit rate | vs 52.00% | drawdown | halted |
  |---|---|---|---|---|---|
  | **fresh holdout** (to 2023-09-13) | 4,663 | **54.31%** | **+2.31%** | 15.07% | **yes, late** |
  | development (to 2024-09-13) | 4,632 | 53.53% | +1.53% | 9.24% | no |
  | later, seen (to 2025-09-13) | 3,590 | 51.14% | -0.86% | 15.16% | yes |
  | later, seen (to 2026-09-13) | 4,087 | 53.94% | +1.94% | 7.72% | no |

  Signal level on the fresh holdout: mean_reversion 55.12% (z +2.2),
  taker_flow 53.51% (z +2.1), both significant.

  What it says. The signal cleared the bar on a year it had never seen. The
  risk block, pinned from a config that bet 235 times a year, was never
  sized for 4,600 and tripped its 15% drawdown limit in two of four years.
  And one of the four years was negative: the year the fade gate alone did
  best is the year the taker gate did nothing. Pooled over the three years
  the values were not chosen on: 53.26% on 12,301 bets, +1.26%, z about
  2.8, with a year-to-year range from -0.86% to +2.31%. The backtest
  subtracts no gas.

  This file and `configs/fade-flow-5m.json` are not edited. Any change --
  to the risk block, the stake, the venue's cost -- is a new hypothesis,
  and the next fresh year is the one ending 2022-09-13.

## Decision 8: risk sizing for a 4,600-bet year -- pending

The halt clause failed because the risk block was sized for 235 bets a
year. With a stake of s (a fraction of bankroll) and N bets, the typical
peak-to-trough drawdown of an even walk is about s x sqrt(N): 0.25% x
sqrt(4,600) is 17%, so a 15% limit halts an ordinary year. Four sizings
were run on the four years already burned for this signal
([run 34755963339](https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python/actions/runs/34755963339));
the year ending 2022-09-13 was not fetched. Hit rates differ slightly from
the pinned rows where the pinned run halted and stopped betting.

| sizing | stake | limit | year | bets | hit rate | vs 52.00% | net P&L | max drawdown | halted |
|---|---|---|---|---|---|---|---|---|---|
| pinned | 0.25% | 15% | 2023 | 4,663 | 54.31% | +2.31% | +62.6% | 15.07% | yes |
| pinned | 0.25% | 15% | 2024 | 4,632 | 53.53% | +1.53% | +37.5% | 9.24% | no |
| pinned | 0.25% | 15% | 2025 | 3,590 | 51.14% | -0.86% | -14.1% | 15.16% | yes |
| pinned | 0.25% | 15% | 2026 | 4,087 | 53.94% | +1.94% | +42.6% | 7.72% | no |
| wider | 0.25% | 30% | 2023 | 4,964 | 54.21% | +2.21% | +64.1% | 15.49% | no |
| wider | 0.25% | 30% | 2025 | 4,085 | 51.30% | -0.70% | -13.3% | 16.20% | no |
| smaller | 0.10% | 15% | 2023 | 4,964 | 54.21% | +2.21% | +20.5% | 6.41% | no |
| smaller | 0.10% | 15% | 2024 | 4,632 | 53.53% | +1.53% | +13.3% | 3.37% | no |
| smaller | 0.10% | 15% | 2025 | 4,085 | 51.30% | -0.70% | -4.7% | 5.83% | no |
| smaller | 0.10% | 15% | 2026 | 4,087 | 53.94% | +1.94% | +14.4% | 2.76% | no |
| both | 0.10% | 30% | all | identical to "smaller": no year came near either limit | | | | | |

The "wider" rows not shown equal the pinned rows (no halt to lift). Gas,
which the backtest does not subtract: at $0.30 a bet and a +1.3% pooled
edge (about +2.5% of stake per bet), a stake has to be near $12 to cover
gas and $25 or more for gas to be a minor cost -- a bankroll of $10,000+
at 0.25% or $25,000+ at 0.10% on predict.fun, or a venue without gas.
The sizing chosen here fixes how far the strategy can fall before it
stops; it does not fix that.
