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

### Decision 10, first value: the bankroll is $1,000

The person building the strategy set the bankroll at $1,000. At that size
the sizing chosen in Decision 8 cannot run: 0.10% of $1,000 is $1 a bet,
under predict.fun's gas, under Polymarket's $5 minimum order and under the
config's own minimum stake, so the validator refuses the config. The
smallest stake each venue accepts is a far larger fraction of $1,000 than
Decision 8 sized for -- $5 on predict.fun is 0.5%, Polymarket's minimum
with the config's 1.5x headroom is $7.50, 0.75% -- with typical drawdowns
of a third to a half of the bankroll in an ordinary year.

Run [34791665012](https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python/actions/runs/34791665012):
the pooled config with the bankroll set to $1,000 and each venue's
smallest stake, on the six years already burned, the drawdown limit set to
50% so each year runs to its end. Stakes compound with the bankroll, which
is why the dollar figures exceed a flat-stake estimate. Nothing here is a
test.

**predict.fun, $5 a bet (0.5%), daily loss limit 4% as pinned.** The
backtest subtracts no gas; the table does, at the $0.30 placeholder.

| year | bets | hit rate | P&L before gas | gas at $0.30 | net after gas | gas per bet that breaks even | max drawdown | days the 4% daily limit stopped play |
|---|---|---|---|---|---|---|---|---|
| 2021 | 4,796 | 52.47% | +115 | -1,439 | -1,323 | $0.02 | 34.8% | 1 |
| 2022 | 5,008 | 53.27% | +711 | -1,502 | -792 | $0.14 | 23.3% | 17 |
| 2023 | 4,959 | 54.20% | +1,490 | -1,488 | +2 | $0.30 | 28.0% | 6 |
| 2024 | 4,625 | 53.49% | +772 | -1,388 | -615 | $0.17 | 16.5% | 10 |
| 2025 | 4,083 | 51.30% | -230 | -1,225 | -1,455 | none | 27.2% | 2 |
| 2026 | 4,081 | 53.95% | +908 | -1,224 | -316 | $0.22 | 14.3% | 6 |

Five of six years lose after gas at $0.30; the sixth breaks even. The
last-but-two column is the gas per bet at which each year would have
broken even: for the strategy to net money at $1,000 on predict.fun, a
real bet has to cost about $0.15 or less. That number has not been
measured. The 4% daily limit ($40) stopped play on up to 17 days a year;
loosening it to 20% changed the year's result by under $100 either way.

**Polymarket, $7.50 a bet (0.75%), daily loss limit loosened to 20%**, for
completeness -- the venue's own rules on where it may be used come first,
and no server location changes them.

| year | bets | hit rate | P&L, no gas | max drawdown |
|---|---|---|---|---|
| 2021 | 404 | 44.31% | -335 | 36.3% |
| 2022 | 5,020 | 53.23% | +1,452 | 33.5% |
| 2023 | 4,962 | 54.17% | +3,789 | 41.3% |
| 2024 | 4,633 | 53.59% | +1,926 | 23.8% |
| 2025 | 3,586 | 51.06% | -337 | 38.6% |
| 2026 | 4,087 | 53.90% | +2,159 | 20.8% |

The 2021 row is a lock-up, not a bad year: an early drawdown took the
bankroll under $667, 0.75% of it fell below the $5 minimum, and no bet
was placed again -- 404 bets, the exact failure the validator's 1.5x
headroom is meant to prevent and cannot at this scale. Drawdowns of 21%
to 41% in the other years. The 4% daily limit stopped play on 57 to 158
days a year at this stake.

What it says. $1,000 is below the scale at which this signal's economics
work on predict.fun unless a real bet costs a fraction of the placeholder
gas, and on Polymarket it works only with a third to half of the bankroll
at risk in an ordinary year and a lock-up in a bad one. The two numbers
that decide the predict.fun case -- gas per bet, and whether the quote is
still near 50/50 when the gates fire -- are measured live at no cost, and
come before any other choice.

### Decision 10, second value: the venue is Polymarket, and its API has been read

Chosen: **Polymarket**, with the process hosted on a small AWS instance in
the UAE region (me-central-1), the user's reason being proximity and a
network they trust to stay up. Polymarket's own terms and the law where
the user is apply wherever the server sits; nothing in this repository is
built to route around either. Options not taken: predict.fun (loses in
five of six seen years at $1,000 after the placeholder gas, and the real
gas is still unmeasured); a US or EU region (further from the user, with
no bearing on the venue's rules).

The read side of the venue was written first, because it needs no key
and can move no money, and its field names were checked against the
live API from a GitHub Actions runner on 2026-09-15 (the sandbox cannot
reach the hosts). What the live payloads said:

- A five-minute BTC window is one market in one event, both at the slug
  `btc-updown-5m-<opening second>`; `/markets?slug=` and `/events?slug=`
  both return it. Windows are listed about a day ahead.
- `endDate` is the window's end. `startDate` is **not** its start but the
  listing time, a day earlier; the first draft read it as the start and
  saw 86,177-second windows. The start now comes from the slug.
- `outcomes`, `outcomePrices`, `clobTokenIds` are JSON-encoded strings;
  the outcomes are `Up` and `Down`; a settled window carries `"1"`/`"0"`.
- `orderMinSize` is 5 shares (the first draft scaled it to 0.5 as if it
  were a price) and the tick is 0.01.
- Resolution: Chainlink's BTC/USD **60-second TWAP** stream, Up on `>=`.
  The April 2026 windows used the spot stream. A one-minute average at
  each end is not the close-to-close return every backtest here settles
  on. Not yet measured; it is measurable from the minute bars.
- The fee, confirmed twice. The market's `feeSchedule` reads `rate 0.07,
  exponent 1, takerOnly true, rebateRate 0.2` (`feeType crypto_fees_v2`),
  and the documentation gives `fee = C × feeRate × p × (1 − p)` with the
  crypto taker rate 0.07, makers never charged, 20% of taker fees rebated
  to makers. The 1.75 cents a share at 0.50 and the 51.75% break-even in
  Decision 10's table stand, on evidence now rather than memory. The
  `makerBaseFee` / `takerBaseFee` of 1000 on the same market are base
  fields the schedule overrides.
- `eventStartTime` on the market (`startTime` on its event) is the
  window's opening second and agrees with the slug; the code reads the
  field first and the slug second.
- Gamma's `outcomePrices`, `bestBid`, `bestAsk` and `lastTradePrice` were
  minutes stale on an open window (`updatedAt` before the window opened)
  while the CLOB book had moved from 0.51 to 0.07. Prices come from the
  book only.
- What a window looks like from inside: at 197 seconds into
  `btc-updown-5m-1789481700` the book was Up 0.06/0.07, Down 0.93/0.94,
  with a one-cent spread and a few hundred shares at the touch on each
  side. The market moves far within a window; the quote that matters is
  the one in the first seconds, which the watcher measures.
- The documentation index now describes pUSD as the trading collateral
  and lists session keys (a separate, scoped, time-limited signer for a
  deposit wallet). Both belong to the deployment step; the second is the
  shape any signer on the server should take.

Nothing in the four pre-registered runs changes: they measured the
signal against Binance closes, and the venue's settlement is a separate
question that gets its own measurement before any money moves.

### The quote, which nothing has measured yet

The four pre-registered runs say the signal predicts the bar. Not one of
them says anybody will sell that bar at a fair price: every backtest here
prices its bets at a quote the backtest invented. The probe's own log
shows why that matters -- three minutes into a window the book was Up
0.06 / 0.07 against Down 0.93 / 0.94, and a 53% signal is worth nothing
at that price.

So `btc5m watch` now records, every window, at 5, 15 and 30 seconds in:
both sides' books, the depth at the touch, the overround, the side the
stack wants, what that side costs with Polymarket's exact per-share fee,
and the edge left. `settle` fills in who won once the venue says, and
`report` splits it all by whether the gates fired. It places nothing and
holds no key.

Three facts it records rather than hides: a window with no market or a
side with no asks still writes a row with the reason; `bar_lag` says
whether the engine read the bar that closed at the window's open, where
0 is the only correct value; and the price is always the CLOB book's best
ask, never Gamma's, which the probe caught minutes stale on an open
window.

One thing that had to be built for it: api.binance.com answers a US cloud
IP with 451, and the flow gate needs Binance's taker-buy volume, which no
other exchange in data.py publishes. Binance's public mirror
(data-api.binance.vision) is now a data source in its own right, and the
watcher falls back to it and says so.

An hour of windows is twelve bets, a standard error of about 14 points on
any hit rate. It answers the quote question, not the edge question, and
the quote question is the one that can end this in an afternoon.

### Where it runs: a small AWS box in me-central-1

Chosen by the user: the UAE region, for proximity and a network they
trust to stay up. Options not taken: a US or EU region (further away, and
no bearing on the venue's rules); a laptop (a watcher that sleeps when
the lid closes measures nothing overnight, which is half the windows).

What is deployed is a read-only service and nothing else. `deploy/` has a
Dockerfile, a watch unit, a settle unit and timer, and a runbook. No
inbound ports, outbound 443, an unprivileged user, one writable
directory, no `Environment=` line in any unit, and no credential anywhere
in the directory -- because the read side needs none and this repository
still has no signer in it.

The clock is the load-bearing part, and the one failure with no
symptom: windows start on five-minute boundaries and the watcher records
the book at +5, +15 and +30 seconds, so a clock ten seconds slow writes
rows that claim an offset they were not read at. The unit starts after
chronyd and the runbook's first check is `chronyc tracking`.

`deploy/` is tested against the code it claims to run: every ExecStart
and the Dockerfile's CMD are parsed with the real argument parser, so a
renamed flag fails the suite rather than silently producing an empty CSV
on a box nobody is watching.

Where the box sits is a hosting choice. Polymarket's own terms and the
law where the user is apply wherever the server is, and nothing here is
built to route around either.

The order of what comes next is fixed by what can end the project
soonest, not by what is most interesting to build:

1. The quote question, which the watcher now answers.
2. Settlement: Polymarket resolves on Chainlink's 60-second TWAP at each
   end of the window; every backtest here settles on Binance
   close-to-close. Measurable from the minute bars already fetched.
3. Only then a signer, in a separate hot wallet, keyed by a session key
   rather than the wallet's own key, with nothing in git.

### The first live hour: the market is 8.5 points from even, not near it

Twelve consecutive windows, 2026-09-15 23:05 to 00:05 UTC, 36 readings
(run 35033962877). The rig is sound: both sides priced on every reading,
no notes, bar lag 0 everywhere, and settle resolved all nine ended
windows. api.binance.com was blocked from the runner as expected and the
mirror answered.

What the book looked like, by seconds into the window:

| seconds in | cheap ask | dear ask | overround | median skew |
|---|---|---|---|---|
| +5s | 0.420 | 0.590 | +0.0100 | 0.085 |
| +15s | 0.455 | 0.555 | +0.0100 | 0.065 |
| +30s | 0.425 | 0.585 | +0.0100 | 0.085 |

Read cheap and dear, not UP and DOWN: which named side is dear varies
window to window, so a median of the UP column averages a dear side with
a cheap one and describes nothing. The report was changed to say cheap
and dear for exactly that reason.

Three findings, in the order they matter.

1. **The market is not near even.** Five seconds in, the typical window
   is already 8.5 points off even; only 4 of 12 were within 5 points.
   Three of the twelve were past the 12-point max_entry_skew limit at +5
   seconds and could not have been entered at all.
2. **So the whole question is which side the stack wants.** All in at
   Polymarket's own fee, the cheap side at 0.420 needs 43.7% and the dear
   side at 0.590 needs 60.7%. A 53% signal clears the first by nine
   points and misses the second by eight. The edge does not need an even
   market; it needs to be on the cheap side. `report` now asks that
   directly: the signal fades the bar that just closed, so if the market
   prices that move continuing, the fade side is the cheap one. Not yet
   answered -- it needs windows where the gates actually fire.
3. **The book is cheap to cross.** The overround was exactly one cent in
   all 36 readings, the book one tick wide on both sides, with 172 to 611
   shares at the touch. A $7.50 stake is 15 shares. Liquidity is not the
   constraint at this size.

No gate fired in twelve windows, which is the expected outcome and not a
finding: the pooled config fires on about one bar in twenty, so twelve
windows expect half a signal and produce none 58% of the time. Ten fired
gates need roughly 230 windows, nineteen hours. A single workflow job
caps at five hours, so the conditional measurement is the box's job.

### Which price settles the bet, and why it may not be the one measured

Polymarket's live market text says it resolves Up if "the TWAP of the time
range specified in the title is greater than or equal to the price at the
beginning of that range", on the btc-usd-twap-60s-streams feed. Every
backtest in this repository settles on Binance 5-minute close-to-close.
Those are not the same sentence, and the text admits four readings:

| rule | compares |
|---|---|
| close_to_close | last price of the window vs the first -- what every number here assumes |
| twap_window | the average over the window vs the price at its start -- the literal reading |
| twap60_ends | the 60-second average at the end vs the 60 seconds before the start |
| twap60_vs_open | the 60-second average at the end vs the price at the start |

`btc5m settlement` measures all four from minute bars. Two things it
reports, in that order, because the first is routinely misread:

1. How often the rules pay different sides. This is not a loss. If the
   paid side flips on a fraction f of windows independently of whether
   the bet was right, a hit rate p becomes p - f(2p - 1): at 53% a 5%
   flip rate costs a third of a point, and it takes a 21% flip rate to
   erase a 1.3-point edge.
2. The pooled config's own hit rate under each rule, on the windows it
   bet. This is the number that decides it, because independence cannot
   be assumed: the windows where the rules disagree are the close ones,
   which is where a five-minute signal does its work.

Every value in this strategy was chosen against close_to_close. If the
venue settles on one of the others, those choices were made against the
wrong target and the four pre-registered runs measured a bet nobody can
place. That is the risk being quantified; it is not yet quantified.

Which sentence the venue actually means is not settled by reading it
again. `settlement --quotes` scores the four rules against windows the
venue itself has resolved, as recorded by `btc5m watch`. Only a window
where the rules disagree is evidence, so the sample accumulates slowly
and this is another thing the always-on box is for.

A choice for the user once the numbers are in: if the rules differ
materially on the bars the config bets, the options are to re-run the
pre-registration against the venue's actual rule, to keep
close_to_close and accept a known bias, or to stop. That is a strategy
decision, not a measurement.
