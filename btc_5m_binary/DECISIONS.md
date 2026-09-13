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

## Decision 8: risk sizing for a 4,600-bet year -- a 0.10% stake, the 15% limit kept

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

- **Chosen:** option b, `risk.max_stake_pct: 0.001` with `max_drawdown_pct`
  unchanged at 0.15. The 15% limit then sits at about 2.3 times an ordinary
  year's drawdown -- a tripwire that means something is wrong -- at a fifth
  of the profit and loss.
- **Not chosen, and why.** a (0.25% stake, 30% limit) removes the halts but
  keeps 16% drawdowns and swings from -13% to +64%; c (0.10% stake, 30%
  limit) behaved identically to b on every year seen and loosens a limit
  that no longer needs loosening.

## The third pre-registered run

- **Config:** `configs/fade-flow-sized-5m.json`, written 2026-09-13 after
  Decision 8 and before any fetch of the fresh holdout. Relative to the
  second config exactly one value differs: the stake per bet.
- **Fresh holdout:** the year ending 2022-09-13. Nothing in this repository
  had read it when the config was written.
- **Pass criterion, fixed before the fetch, unchanged:** on the fresh
  holdout the backtest's hit rate beats the 52.00% break-even by at least
  +1.0%, significant at two standard errors, with no halt. Anything less
  fails.
- **Seen-year reference at this sizing** (run 34755963339): 2023 +2.21%,
  2024 +1.53%, 2025 -0.70%, 2026 +1.94%; no halts, drawdowns at most 6.41%.
- **Result** ([run 34756636545](https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python/actions/runs/34756636545),
  the first and only run): **FAIL**, by the letter, on the significance
  clause. On the fresh holdout the config placed 5,020 bets at 53.23% +/-
  0.70%, +1.23% over break-even -- above the +1.0% floor, no halt, drawdown
  5.46% -- but 1.8 standard errors above break-even, not 2. The clause
  needed about +1.4% on that many bets; the year delivered +1.23%.

  Five years, one signal (the stake change moves P&L and drawdown, not the
  hit rate):

  | year | status | bets | hit rate | vs 52.00% | on its own |
  |---|---|---|---|---|---|
  | **to 2022-09-13** | **fresh, this run** | 5,020 | **53.23%** | **+1.23%** | z 1.8, not significant |
  | to 2023-09-13 | fresh for the second run | 4,964 | 54.21% | +2.21% | significant |
  | to 2024-09-13 | development | 4,632 | 53.53% | +1.53% | significant |
  | to 2025-09-13 | seen | 4,085 | 51.30% | -0.70% | negative |
  | to 2026-09-13 | seen | 4,087 | 53.94% | +1.94% | significant |

  Signal level on the fresh year: taker_flow 53.34% (z +1.9),
  mean_reversion 53.34% (z +1.0); both positive, neither significant alone.

  What it says. Pooled over the four years the values were not chosen on
  (every year but the development one): 9,638 wins in 18,109 graded bets,
  53.22%, +1.22%, about 3.3 standard errors above break-even, with a
  year-to-year range from -0.70% to +2.21%. The two pre-registered fresh
  years alone pool to 53.71% on 9,962 bets, +1.71%, z about 3.4. The edge
  looks real and small; a single year of 5,000 bets has a standard error
  of 0.7%, so a per-year bar of "significant at two standard errors"
  needs +1.4% and a true +1.2% edge clears it less than half the time.
  That is a statement about the bar's power, made after reading the
  result, and so it cannot rescue this run: the run fails. It can shape
  the next pre-registration, if there is one, before the next fresh year
  (the one ending 2021-09-13) is fetched.

  This file and `configs/fade-flow-sized-5m.json` are not edited.

## Decision 9: the bar -- pooled over three fresh years

- **Chosen** (option 2 of stop / pool / watch quotes): no value of the
  strategy changes. The bar is pooled over three fresh years under one
  config: the year ending 2021-09-13, never fetched, and the years ending
  2022-09-13 and 2023-09-13, each the fresh holdout of an earlier run and
  burned for everything but this pooling.
- **Why.** A single year of 5,000 bets has a standard error of 0.7%; a
  per-year two-sigma bar needs +1.4% and a true +1.2% edge clears it less
  than half the time. Two of three pre-registered runs failed on exactly
  that kind of clause while pooling to +1.71% at 3.4 standard errors.
- **What the bar asks, written before the fetch.** Given the two years
  already known (53.71% on 9,962 bets), the new year must come in at about
  52.4% or better. If the signal were worthless the bar would still pass
  about one time in four on the strength of the earlier years; if the edge
  is +1.2% it passes about seven times in eight. Both numbers are in the
  config so the result is read with them.

## The fourth pre-registered run

- **Config:** `configs/fade-flow-pooled-5m.json`, every value byte-for-byte
  the sized config's, written 2026-09-13 before any fetch of the year
  ending 2021-09-13.
- **Pass criterion, fixed before the fetch:** pooled over the three fresh
  years, (a) hit rate above 52.00% by at least +1.0%, (b) by at least 2
  standard errors of the pooled estimate, (c) the year ending 2021-09-13
  does not halt. Anything less fails.
- **Reference, outside the bar:** the years ending 2024-09-13, 2025-09-13
  and 2026-09-13.
- **Result** ([run 34757815880](https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python/actions/runs/34757815880),
  the first and only run): **PASS**, by the letter, and by the thinnest
  margin the bar allowed. The fresh year placed 4,797 bets at 52.48% +/-
  0.72%, +0.48% over break-even, no halt, drawdown 7.88% -- right at the
  52.4% the bar asked of it. Pooled over the three fresh years: 7,868 wins
  in 14,758 graded bets, 53.31%, +1.31% over break-even, standard
  error 0.41%, z +3.2. Clauses (a), (b) and (c) all hold.

  Six years, one signal:

  | year | status | bets | hit rate | vs 52.00% |
  |---|---|---|---|---|
  | **to 2021-09-13** | **fresh, this run** | 4,797 | **52.48%** | **+0.48%** |
  | to 2022-09-13 | fresh, third run | 5,020 | 53.23% | +1.23% |
  | to 2023-09-13 | fresh, second run | 4,964 | 54.21% | +2.21% |
  | to 2024-09-13 | development | 4,632 | 53.53% | +1.53% |
  | to 2025-09-13 | seen | 4,085 | 51.30% | -0.70% |
  | to 2026-09-13 | seen | 4,087 | 53.94% | +1.94% |

  Signal level on the fresh year: mean_reversion 54.48% (z +1.8),
  taker_flow 52.31% (z +0.4). The taker gate did almost nothing in
  2020-21; the fade gate carried what there was.

  What it says. The pass is real and it is thin: the new year sits at the
  edge of the range a worthless signal would produce, and the config said
  in advance that such a signal passes this bar one time in four. Read
  with the earlier runs rather than instead of them: pooled over the five
  years the values were not chosen on, 12,155 wins in 22,905 graded bets,
  53.07%, +1.07% over break-even, z +3.2, with a 95% range of
  about +0.4% to +1.7% and a year-to-year spread from
  -0.70% to +2.21%. The best estimate of the edge is about +1.1% of hit
  rate, or +2% of stake per bet, before gas. Four pre-registered tests:
  two passes, two fails on a single clause each; every one of them ended
  above break-even.

  This file and `configs/fade-flow-pooled-5m.json` are not edited. The
  program of fresh-year tests on this signal ends here; what remains is
  economics -- bankroll, venue, gas -- and the live-quote question, neither
  of which a backtest can answer.

## Decision 10: venue, bankroll and stake -- pending

The signal is settled as far as backtests can settle it: about +1.1% of hit
rate, 95% range roughly +0.4% to +1.7%, some 4,600 bets a year. Whether
that is money depends on three things no backtest contains: the venue's
cost, the gas per bet, and the bankroll. The package's own venue rules give
the first two (`btc5m/venue.py`; predict.fun's $0.30 gas is a placeholder
to be measured on a real transaction, and its 200 bps is read on face value,
the pessimistic reading).

| | predict.fun | Polymarket taker | Polymarket maker |
|---|---|---|---|
| a 0.50 share really costs | 0.5200 | 0.5175 | 0.5000 |
| gas per bet | $0.30 | none | none |
| profit per $ staked at 53.07% | +2.06% | +2.55% | +6.14% |
| at the low end, 52.4% | +0.77% | +1.26% | +4.80% |
| at the high end, 53.7% | +3.27% | +3.77% | +7.40% |

A maker fill on Polymarket is not guaranteed -- posting at 0.50 after a push
means the other side may already have left -- so the maker column is an
upper bound, not a plan.

At the centre estimate, 4,600 bets a year, before anything the live
quote may take away:

| stake per bet | predict.fun net per bet | per year | Polymarket taker net per bet | per year | bankroll at 0.10% | bankroll at 0.25% | typical drawdown |
|---|---|---|---|---|---|---|---|
| $5 | -0.20 | -907 | +0.13 | +587 | $5,000 | $2,000 | $339 |
| $10 | -0.09 | -433 | +0.26 | +1,173 | $10,000 | $4,000 | $678 |
| $25 | +0.21 | +986 | +0.64 | +2,933 | $25,000 | $10,000 | $1,696 |
| $50 | +0.73 | +3,353 | +1.28 | +5,867 | $50,000 | $20,000 | $3,391 |
| $100 | +1.76 | +8,085 | +2.55 | +11,733 | $100,000 | $40,000 | $6,782 |

The typical drawdown is stake x sqrt(bets), the figure Decision 8 was
sized on: at 0.10% of bankroll it is about 7% of it, at 0.25% about 17%.

What the table says. On predict.fun the gas has to be paid out of a
+2% edge, so a $5 or $10 stake loses money and a $25 stake earns about
$1,000 a year; at the low end of the edge's range, predict.fun only covers
its gas above a stake of about $39. Polymarket has no gas and a
cheaper fee, so it is positive at every stake and at every point of the
range. The stake is set by the bankroll and the sizing already chosen: a
$25 stake at 0.10% is a $25,000 bankroll; at 0.25% it is $10,000 with the
wider drawdown that sizing carries. None of these figures survive a live
quote that has already moved off 50/50 when the gates fire, which is the
next thing to measure, along with the real gas.
