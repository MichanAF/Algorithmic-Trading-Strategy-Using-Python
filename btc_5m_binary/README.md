# BTC 5-minute binary strategy

One question, asked every five minutes: **will BTC close higher or lower than it
is right now? Yes or no.**

A binary bet has no stop loss, no exit and no partial fill. Once it is placed the
whole stake is at risk until expiry. That collapses the strategy into two
decisions, and this repository is those two decisions:

| | |
|---|---|
| **Should we bet?** | A stack of 3-5 gates must each pass, then three betting conditions must hold. |
| **How much?** | Three risk conditions size the stake, or refuse it. |

Everything is auditable. Every gate reports its own sub-checks, so a refused bar
names the exact condition that stopped it.

---

## Start with the arithmetic, not the indicators

Over five minutes BTC is close to a coin flip. On the bundled test data the
up-bar base rate is 49.7%, and the lag-1 autocorrelation of 5-minute returns is
about -0.03. There is no large directional signal to find.

That means the payout, not the prediction, sets the bar:

| Payout on a win | Break-even hit rate | What you need |
|---|---|---|
| 2.00x (net 1.00) | 50.0% | any edge at all |
| 1.95x (net 0.95) | 51.3% | +1.3 points |
| **1.90x (net 0.90)** | **52.6%** | **+2.6 points** |
| 1.80x (net 0.80) | 55.6% | +5.6 points |
| 0.52 contract + 20bps fee | 52.2% | +2.2 points |

At 1.90x a 52% strategy loses money. This is why the second betting condition
compares the modelled probability against the break-even the payout implies,
and refuses the bet when the edge is not there. Get your venue's real payout
into the config before tuning anything else.

The strategy's answer to a near-coin-flip is **selectivity**: require many
independent conditions to agree, bet rarely, and stay out otherwise. The default
stack fires about 6 times a day out of 288 bars.

---

## Quick start

```bash
pip install numpy                 # the engine needs nothing else

# See the machinery work on generated bars
python -m btc5m signal   --synthetic 20000      # full gate trace for one bar
python -m btc5m backtest --synthetic 20000      # walk-forward run

# Then point it at real history
python -m btc5m fetch    --exchange binance -o btc_5m.csv
python -m btc5m backtest --data btc_5m.csv --config configs/default.json
```

Two commands matter more than the backtest, and they are what turn the gate
menu below from opinion into evidence:

```bash
python -m btc5m gates   --data btc_5m.csv     # what is each gate's vote worth?
python -m btc5m compare --data btc_5m.csv     # which stack should I run?
python -m btc5m overlap --data btc_5m.csv     # are the gates actually independent?
python -m btc5m quote   --data btc_5m.csv --down 51   # price a live market
```

From Python:

```python
from btc5m import config_from_dict, load_csv, run_backtest, SignalEngine, build_features

cfg = config_from_dict({"betting": {"net_payout": 0.95}})
series = load_csv("btc_5m.csv")
print(run_backtest(series, cfg).summary())

engine = SignalEngine(cfg)
signal = engine.evaluate(build_features(series, cfg), len(series) - 1)
print(signal.report())          # every gate, every check, then the verdict
```

---

## The gate stack

Gates come in three kinds, and they run in this order:

1. **Veto gates** have no directional opinion. They decide whether the market is
   fit to bet on at all. One failure kills the bar, cheaply.
2. **Directional gates** vote UP or DOWN and carry a 0-1 score. They must agree.
3. **Confirmation gates** are told the side the directional gates chose and may
   only veto it, never propose their own.

That third kind exists because of a mistake worth repeating. The location gate
originally voted for whichever side had more room before the nearest swing
level. It tested at 44% accuracy, because "more room below" is a mean-reversion
opinion, and it spent its time fighting the trend gates. "Do not buy into
resistance" is a veto on a long, not a reason to go short.

The **default stack is five gates**, two vetoes and three directional:

| # | Gate | Kind | The question it answers |
|---|---|---|---|
| 1 | `data_integrity` | veto | Is the feed trustworthy? No gapped bar, stuck price, blown-out spread or zero-volume bar. |
| 2 | `volatility_regime` | veto | Is there enough movement to pay for the spread, without being chaos? ATR percentile inside a band, and above a floor in basis points. |
| 3 | `trend_alignment` | directional | Which way, and does the 15-minute chart agree? EMA stack, higher-timeframe slope, regression fit, VWAP side. |
| 4 | `persistence` | directional | Does this regime extend moves or reverse them? Variance ratio above 1, ADX above a floor, +DI vs -DI for the side. |
| 5 | `participation` | directional | Is real volume behind it? Above median, below blow-off, with on-balance-volume agreeing. |

Note what is **not** in the default stack: `momentum_thrust`, the obvious
"is it moving right now" gate. On the test data its vote is anti-predictive over
the very next bar, because 5-minute BTC returns are mildly negatively
autocorrelated: a sharp three-bar push is more often followed by a give-back
than a continuation. `persistence` earns the slot instead by asking whether
momentum is the right tool at all before anyone follows anything.

Keep it. Do not take it on faith. Measure it on your own data.

---

## The gate menu: what you can factor for

All of these are implemented and selectable. Pick 3-5.

### Trend and bias, picks the side
| Gate | Factors |
|---|---|
| `trend_alignment` | EMA 9/21/50 stack, 15-minute EMA slope, 20-bar regression slope and R-squared, rolling VWAP side |

Other factors in this family worth adding if your data supports them: hourly
trend as a third timeframe, pivot or opening-range position, daily VWAP.

### Regime, decides whether following or fading is correct
| Gate | Factors |
|---|---|
| `persistence` | Lo-MacKinlay variance ratio, ADX, +DI/-DI |
| `volatility_regime` | ATR percentile rank, ATR in basis points, Bollinger width percentile |

The variance ratio is the cheap stand-in for a rolling Hurst exponent. Above 1,
moves extend; below 1, following them is paying the spread to be wrong.

### Momentum and thrust
| Gate | Factors |
|---|---|
| `momentum_thrust` | 3-bar rate-of-change z-score, RSI band with a ceiling, MACD histogram expansion, candle body dominance |

The RSI **ceiling** is the half people leave out. A thrust that already ran to an
extreme is the tail of a move, not the start of one.

### Volume and participation
| Gate | Factors |
|---|---|
| `participation` | Bar volume vs rolling median, on-balance-volume slope, body agreement, blow-off ceiling |

If you have trade-level data, this is the gate to upgrade: cumulative volume
delta and taker buy/sell imbalance are strictly better than candle-derived
proxies. The interface is `Gate._evaluate`; add the feature in `features.py`.

### Location and structure
| Gate | Factors |
|---|---|
| `location` (confirm) | Distance to the prior swing high/low in ATR, extension from VWAP in ATR |

Warning from the test data: this gate suits range and mean-reversion stacks. It
conflicts with a trend stack by construction, because a trending market is
supposed to be near its recent extreme. Adding it to the default stack cut
signals to 0.8/day and the edge went negative.

### Clock and session
| Gate | Factors |
|---|---|
| `session` (veto) | Allowed UTC hours, perp funding blackout at 00/08/16 UTC, optional weekend skip |

Roughly free: it removed about 2% of signals and slightly improved the hit rate.
Add your own macro-event blackout here (CPI, FOMC) if you trade through them.

### Cross-asset
| Gate | Factors |
|---|---|
| `cross_asset` (confirm) | Reference asset rate-of-change, direction agreement |

ETH usually leads BTC on impulsive 5-minute moves. Needs `--reference ETH.csv`;
it says so loudly rather than silently passing when the data is missing.

### The other personality
| Gate | Factors |
|---|---|
| `mean_reversion` | Close z-score, RSI exhaustion, exhaustion volume, variance ratio below 1 |

Swap this in for `trend_alignment` and the stack fades moves instead of
following them. It cannot be combined with `persistence`: one needs a trending
variance ratio and the other a mean-reverting one, so a stack holding both never
fires. That is by construction, not a bug.

**Honest status: unproven.** On the test data it shows +5.6% on 67 bets in 417
days, which is statistically silent. Loosening the thresholds to get more signals
turns it significantly negative. It is here so you can measure it.

### Factors worth adding that are not here
These need data this repo does not fetch, and each is a real edge source for
short-horizon direction:

- **Order book imbalance** at the top few levels, and spread width as a veto.
- **Perp funding rate and open-interest delta**, for positioning.
- **Liquidation clusters** above and below, for where a move accelerates.
- **Exchange netflows and stablecoin flows**, slower but directional.
- **A calibrated probability model** (logistic or gradient-boosted) over these
  features, replacing the conviction-to-probability map described below.

---

## The three betting conditions

Gates decide whether the market is worth an opinion. These decide whether that
opinion is worth money. All three must hold. They live in `signal.py`.

### 1. Gate agreement
Every veto gate passes, the directional gates all point the same way, no
confirmation gate refuses that side, and the blended conviction clears
`min_conviction` (default 0.60).

Two modes. `unanimous` requires every directional gate to pass and agree.
`weighted` takes a weighted vote and can trade with dissent if you let it, using
`gate_weights` for per-gate weighting.

### 2. Priced edge
```
p_model  >=  break_even(payout, fees)  +  required_edge
```
Conviction maps onto a probability by a plain monotone map: `0.5 + (prob_cap -
0.5) x conviction`, default `prob_cap` 0.64. This is deliberately not a fitted
model. The backtest prints a **calibration table** of realised hit rate per
conviction bucket, and that table is what tells you whether `prob_cap` is honest
for your data. Start conservative and raise it only when the realised numbers
support it.

Default `required_edge` is 0.03. That margin is not decoration: it is the buffer
against `prob_cap` being optimistic, which it usually is.

**This condition and condition 1 are not independent.** Both are thresholds on
conviction, and at the default payout the conviction floor is the tighter of the
two, so this one never refuses a bet on its own. It takes over below about 1.83x.
See "Overlap analysis" for the measurement and `effective_conviction_floor()` for
the single number that actually applies.

### 3. Timing
The signal must be fresh (`max_signal_age_seconds`, default 45) and there must be
enough time left before expiry for the move to happen
(`min_seconds_to_expiry`, default 60). On a hard five-minute clock a signal
acted on at 4:30 is a different bet from the same signal at 0:05.

---

## The three risk-management conditions

A binary bet cannot be stopped out, so every control has to act before the bet
exists. That leaves exactly three levers. They live in `risk.py`, and each
returns the same auditable checks the gates do.

### 1. Stake sizing: capped fractional Kelly
```
kelly  = (p x odds - (1 - p)) / odds
stake  = bankroll x min(kelly x kelly_fraction, max_stake_pct)
```
Defaults: `kelly_fraction` 0.25, `max_stake_pct` 0.02. Kelly sizes the bet to the
edge; the cap is what survives the edge being overestimated, which it will be.
At realistic edges the **cap binds, not Kelly** — that is intended. Negative
Kelly means no bet at any size.

### 2. Loss limits and exposure
Five bounds, because consecutive five-minute bets are correlated in a way a
single-bet limit does not capture:

| Control | Default | Why |
|---|---|---|
| Daily loss cap | 5% of the day's opening bankroll | Bounds one bad session. Resets on the UTC day. |
| Max drawdown | 20% peak-to-trough | Permanent halt, not a pause. |
| Consecutive losses | 3, then 6 bars of cooldown | A losing streak usually means the regime turned. |
| Bets per hour | 4 | Stops the stack betting the same move four ways. |
| Concurrent bets | 1 | Overlapping bets on one move are one position, not two. |

### 3. Strategy health
The control that notices the edge has stopped existing, which no amount of
position sizing will save you from.

Rolling hit rate over the last 30 bets is compared against the break-even the
payout actually implies, minus a 4-point buffer. Below that, the stake is halved
(or betting halts, with `halt_when_unhealthy`). Separately, an ATR percentile
above 0.985 is a volatility shock and the strategy stands down entirely.

Needs 15 graded bets before it will judge anything, and says so until then.

**One tension worth knowing about.** The drawdown halt is permanent and measured
from peak equity, while stakes scale with the bankroll. On a compounding run it
will eventually fire: the bundled 417-day reference run grew the bankroll, then
gave back 21% from its peak and stopped for good. That is the control working as
designed, not a bug, but it means resuming is a decision a person makes, not
something the engine does quietly. Raise `max_drawdown_pct`, or size from a fixed
notional rather than the live bankroll, if you want different behaviour.

---

## Choosing a stack: measure, do not guess

`btc5m compare` scores candidate stacks on the same data. On the bundled test
fixture, 120,000 bars (417 days):

| Stack | Gates | Signals/day | Accuracy | vs break-even | z |
|---|---|---|---|---|---|
| **default** (trend + persistence + participation) | 5 | 3.62 | **59.80%** | **+7.17%** | **+5.7** |
| trend6-session (adds the session veto) | 6 | 3.55 | 59.84% | +7.21% | +5.7 |
| core3 (trend alignment only) | 3 | 55.98 | 54.06% | +1.43% | +4.4 |
| thrust (momentum instead of persistence) | 5 | 2.24 | 49.84% | -2.79% | -1.7 |
| meanrev | 3 | 0.43 | 51.38% | -1.25% | -0.3 |
| everything | 7 | 0.00 | n/a | n/a | 1 signal in 417 days |

Three lessons, and they are the reason the tool exists:

**More gates is not better.** The seven-gate `everything` stack fires twice in
417 days. A stack that cannot produce a sample cannot prove anything, and it
cannot be traded. This is why the config validator caps a stack at seven and
targets 3-5.

**Fewer is not better either.** `core3` fires 30 times a day but its edge is
thin. Selectivity is the product.

**A gate can be worth more in combination than alone.** `participation` scores
about 49.6% on its own vote, below a coin flip. Adding it to
`trend_alignment + persistence` still improved the stack by roughly two points.
It is a good filter and a bad predictor. `btc5m gates` finds the predictors;
only `btc5m compare` finds the filters.

Per-gate votes on the same data:

| Gate | Signals/day | Accuracy | vs break-even | z | Verdict |
|---|---|---|---|---|---|
| `trend_alignment` | 156.2 | 53.47% | +0.84% | +4.3 | edge, significant |
| `persistence` | 101.6 | 53.14% | +0.51% | +2.1 | edge, significant |
| `location` (confirm) | 0.1 | 59.65% | +7.02% | +1.1 | too few to judge |
| `participation` | 82.4 | 49.79% | -2.84% | -10.5 | negative, significant |
| `momentum_thrust` | 44.7 | 49.63% | -3.00% | -8.2 | negative, significant |
| `mean_reversion` | 3.0 | 45.03% | -7.60% | -5.4 | negative, significant |
| `cross_asset` | 0.0 | n/a | n/a | n/a | needs `--reference` |

Note how small the individual edges are. The two gates that work contribute
under a point each on their own. The stack's +7.17% comes from requiring them to
agree, not from any one of them being clever.

Read `z` before `accuracy`. It is how many standard errors the hit rate sits
above break-even. Under +2, the gate has shown you nothing yet.

---

## Overlap analysis: are the gates asking different questions?

A stack of five gates is only as selective as the number of **independent**
questions it asks. Two gates that fire together for the same underlying reason
do not double the evidence. They halve the signal count while adding nothing,
and make the stack look more confirmed than it is.

```bash
python -m btc5m overlap --data btc_5m.csv
```

Four measurements, weakest to strongest. The third is the one that decides.

| | What it asks |
|---|---|
| **Structural** | Do two gates read the same feature? A hard floor: gates sharing an input cannot be independent. |
| **Verdict** | Do they pass on the same bars, and vote the same way? Gates can share no features and still be near-duplicates. |
| **Marginal value** | Among bars where every *other* gate already agreed, does this gate's verdict still separate winners from losers? |
| **Leave-one-out** | What the whole stack does without each gate. |

### What it found, and what changed as a result

| Overlap | Finding | Action |
|---|---|---|
| Gate inputs | The five default gates read **disjoint** feature sets | none needed |
| `trend_alignment` vs `persistence` | Independent on *when* they fire (phi +0.07) but **99% agreement on direction** | documented: read this stack as two direction votes plus one regime filter, not three votes |
| `participation` marginal value | **+1.5 points, z = +1.9** — no information once the others agreed | added the `trend4` preset without it |
| Betting conditions 1 and 2 | `priced_edge` **never binds** at default settings | added `effective_conviction_floor()`; the report now names the single real threshold |
| Betting condition 3 | `timing` is **not testable** on historical bars | reported as such rather than counted as passing |
| Kelly vs stake cap | The cap binds at **every** allowed conviction, so `kelly_fraction` never sizes a bet | the stake check now names which half bound |
| `daily_loss_limit` vs `max_drawdown` | The halt flag made **both** fail together, double-counting every post-halt bar | fixed: the halt belongs to `max_drawdown` alone |
| Conviction vs outcome | **Non-monotone**: the top bucket is the worst | documented below; do not trust `prob_cap` |

Two of these deserve spelling out.

**Betting conditions 1 and 2 are one condition.** Both are thresholds on the
same scalar, because `p_model` is a monotone function of conviction. At the
default 1.90x payout the edge requirement is satisfied from conviction 0.402
upward, while the conviction floor already demands 0.600 — so condition 2 can
never be the reason a bet is refused. They are still both worth keeping, because
**which one binds moves with the payout**: below about 1.83x the edge requirement
takes over and becomes the tighter test. But nobody should read them as two
independent safeguards. The report now prints the single effective floor.

**`participation` does not earn its slot.** Its directional verdict adds nothing
once the other gates agree, and it removes about 80% of the remaining signals to
buy roughly 1.5 points of accuracy. Sized so each stack risks the same fraction
of bankroll per day, over the same year:

| Stack | Per-bet stake | Bets/day | Hit rate | Max drawdown | Sharpe |
|---|---|---|---|---|---|
| 5 gates (default) | 1.05% | 3.7 | 57.47% | 15.85% | 3.61 |
| **4 gates, no `participation`** | 0.18% | 18.5 | 56.36% | **6.95%** | **6.18** |
| 3 gates (`core3`) | 0.07% | 43.7 | 54.21% | 5.14% | 4.01 |

Same answer when each stack is sized at its own quarter-Kelly instead. The
`trend4` preset is that four-gate stack. The default is left as it is because
this is one synthetic fixture and the finding sits close to the significance
bar — but measure it on your own data before keeping the fifth gate.

### The trap this analysis walked into first

Comparing stacks by accuracy alone reverses the answer, and so does comparing
them by raw profit. At a **fixed** 2% stake, the loose stacks all hit the 20%
drawdown halt within days, because 43 bets a day at 2% risks most of the
bankroll daily. The first comparison run looked like this:

| Stack | Bets placed | Net P&L | Halted on |
|---|---|---|---|
| 5 gates | 730 | +36,611 | day 204 of 365 |
| 4 gates | 297 | +6,196 | day 24 |
| 3 gates | 148 | +1,355 | day 9 |

Read at face value that says selectivity wins by a mile. It does not. All three
tripped the halt, and the looser ones tripped it in the **first few weeks** — so
those numbers mostly measure how fast each stack hit a risk limit calibrated for
about four bets a day, not how good the stack is. At 2% a bet, 43 bets a day puts
most of the bankroll at risk daily; the three-gate stack was finished on day 9.

**Stake and signal frequency have to be varied together.** That is itself an
overlap, between the betting layer and the risk layer, and it is the most
expensive one in this repository: it inverts the conclusion.

### Read `z`, not the headline

The `verdict` column is deliberately conservative. Under 100 joint passes, or
under 50 bars in either bucket, it reports "too few to judge" rather than a
number. A gate that looks brilliant on 57 signals in a year has told you
nothing.

---

## One-year backtest

The default five-gate stack, default risk settings, 105,120 bars (365 days) of
the bundled fixture at seed 11:

```
  bars evaluated        104,832
  tradable signals      1,394
  bets placed           730 (2.01/day)
  wins / losses / void  429 / 301 / 0
  hit rate              58.77% +/- 1.82%
  break-even needed     52.63% (payout 0.900x)
  edge vs break-even    +6.14%   [significant at 2 s.e.]
  net P&L               +36,610.70 (+366.11% on 10,000)
  max drawdown          20.69%
  longest loss streak   6
  annualised Sharpe     3.37
  HALTED                drawdown 20.69% hit the 20.00% limit
```

Reproduce with:

```bash
python -m btc5m backtest --synthetic 105120 --seed 11
```

**Do not read the +366% as a return forecast.** Three reasons, in order of size:
the data is synthetic; the stack was selected on this same fixture; and the run
**halted partway through the year**, so the figure is not even a full year of
this strategy. The honest numbers from this run are the hit rate against
break-even, and the two problems below.

### Problem 1: conviction does not predict accuracy

| Conviction bucket | Bets | Claimed | Realised | P&L |
|---|---|---|---|---|
| 0.60 - 0.70 | 315 | 59.1% | 58.4% | +9,526 |
| 0.70 - 0.80 | 230 | 60.4% | **63.9%** | +26,797 |
| 0.80 - 0.90 | 123 | 61.9% | 56.1% | +2,427 |
| 0.90 - 1.00 | 62 | 63.3% | **46.8%** | **-2,138** |

The most confident bucket is the **only losing one**, and it is 16 points below
what the model claimed. The relationship is not weakly calibrated, it is
non-monotone: past about 0.8, more conviction is worse.

The likely cause is the same one that sank `momentum_thrust`. Maximum conviction
means every gate is maximally extended at once — steep slope, high ADX, heavy
volume — and that is a late-stage trend, which reverts. So `prob_cap` is a
fiction at the top of its range.

It is not currently costing money, for a reason worth noticing: because the
stake cap binds at every conviction, every bet is the same size, so the strategy
does **not** bet more on its worst bucket. The 2% cap is quietly doing the job
`prob_cap` was supposed to do. If you raise the cap so Kelly starts sizing, that
protection disappears and this miscalibration starts allocating real capital to
the worst bets. Fix the calibration before touching the cap.

### Problem 2: the drawdown halt is not a year-long setting

The run stopped betting on **day 204 of 365**, having taken the bankroll from
10,000 to a peak of 58,774 and then given back 20.69% of that peak. The last 160
days produced 605 signals and not a single bet.

Stakes scale with the bankroll, so on any compounding run a fixed percentage
drawdown measured from peak equity will eventually fire. That is the control
working as designed, and resuming should be a person's decision rather than
something the engine does quietly. But it means **a one-year backtest of this
config is not a year of trading**. If you want continuous operation, either raise
`max_drawdown_pct`, or size from a fixed notional rather than the live bankroll.

---

## Running against a real venue (Trust Wallet, BNB Smart Chain)

The "Bitcoin Up or Down" five-minute markets settle from the Chainlink BTC/USDT
top-of-book stream. Their rules map onto this engine exactly:

| Venue rule | What it means here |
|---|---|
| Start price is the beginning of the range, end price the close of the last 5m candle in it | **One 5-minute bar's return**, which is `horizon_bars: 1` |
| Candles labelled by open time, so a market ending 8:20 settles on the 8:15 candle's close | Bar `i` predicts `close[i+1]` vs `close[i]`. Already the model. |
| Equal prices resolve 50-50 | `tie_policy: void`. At BTC precision this is about 1 bar in 13,000. |
| Chainlink mid-price from Binance top of book | See "price source" below |
| Quoted as a percentage | `payout_mode: contract_price` |

```bash
python -m btc5m quote --data btc_5m.csv --down 51 --into-window 8
```

`configs/trustwallet-bnb-5m.json` is the matching config.

### Break-even is the price you pay

This is the whole game on a contract market, and it is harsher than fixed odds.
Buy a side at 0.84 and you need to be right 84% of the time to break even.

Our probability model is capped at `prob_cap` (0.64), so there is a hard ceiling
on what it can ever justify buying:

| Conviction | p_model | Highest price we can pay |
|---|---|---|
| 0.60 (the floor) | 0.584 | 0.54 |
| 0.80 | 0.612 | 0.57 |
| 1.00 (maximum) | 0.640 | 0.60 |

**We can never buy a side priced above about 0.60.** In the screenshot, DOWN at
84% is untouchable and always will be. That is not a limitation to tune away: a
model that claims 85% confidence in a five-minute BTC direction is lying.

### The window clock is the part that will cost you money

These markets trade continuously across the five-minute window, so the quote
drifts from roughly 50/50 at the open toward the realised answer at the close.
The screenshot is a market 253 seconds in, with 47 seconds left, sitting at
84/16. That is not an opportunity. It is the market telling you the move already
happened while your signal went stale.

Our edge exists at **one moment**: the bar close that opens the window, when the
quote is still near even and the next five minutes are genuinely unknown. So the
venue layer adds three vetoes on top of the normal betting conditions:

| Veto | Default | Why |
|---|---|---|
| `entry_window` | within 45s of the open | After that, part of the move you are betting on is already history |
| `time_to_expiry` | at least 60s left | Below that you are betting on the tail of the window, not the window |
| `market_not_decided` | quote skew ≤ 0.12 | A lopsided quote means the market knows something the signal does not |

Same bar, same stack, two different quotes:

```
253s in, 84/16 → NO   (entry_window, time_to_expiry, market_not_decided all fail)
  8s in, 51/49 → YES  buy DOWN at 0.510, edge +0.099
```

**The opposite side is deliberately not taken.** When the quote is lopsided the
other side looks cheap, and the tool reports its edge — but taking it means
betting against your own signal on the strength of a price that moved because
you were late. That is exactly what a stale signal feels like from the inside.

### Gas is a first-order cost, not a rounding error

Every bet is a BNB Smart Chain transaction. Gas is charged per bet regardless of
size, so on a small stake it eats the entire edge. The venue layer refuses any
bet whose expected profit does not cover it, and `minimum_viable_stake()` tells
you where the floor is:

```
minimum viable stake at this edge: 4.63 (3x the 0.30 gas cost)
```

Set `gas_cost_quote` to what you actually pay. Measure it; do not guess. The
same applies to the **overround**: if UP and DOWN together cost more than 1.00,
that excess is the venue's margin and you pay it on every bet. Pass it with
`--overround` and watch what it does to the edge before committing.

### Price source

Settlement uses the Chainlink mid-price (the average of Binance's best bid and
ask), not the last traded price. For **live** decisions read the mid, via
`fetch_topofbook_mid()` or the Chainlink stream directly.

For **backtesting** it does not matter. Binance's BTCUSDT spread is about one
cent, so mid and last differ by at most half a cent, while the median 5-minute
move is about 32 dollars. Only 0.008% of bars move less than half a spread.
Ordinary klines are fine for history.

### What is not built: signing and sending

The engine decides. It does not place orders, and the gap between those is real:

- Placing a bet means signing a BNB Smart Chain transaction against the market
  contract, which needs the contract address and ABI, a funded wallet, and a
  Web3 connection.
- **Never paste a seed phrase or private key into this repository, a terminal,
  a chat, or any tool.** Trust Wallet's recovery phrase controls every asset in
  the wallet, not just a trading balance. Anything that asks for it is a theft.
- If you automate this, do it from a **separate hot wallet** holding only what
  you are prepared to lose, with its key in an environment variable or a
  hardware signer, never in source control.
- Latency matters more than it looks. Our entry window is 45 seconds; BSC block
  time is about 3 seconds and a congested mempool can eat that margin. Measure
  the round trip before trusting the `entry_window` default.

The honest intermediate step is to run `quote` manually against live markets and
place the bets by hand for a few weeks. It costs nothing, it tests the part most
likely to be wrong — whether the quote is ever near even when the signal fires —
and it produces the data you would need to justify automating anything.

---

## What these numbers do and do not mean

The bundled data comes from `btc5m.data.synthetic`, a seeded generator. It is
calibrated so 5-minute returns show roughly the autocorrelation BTC actually
shows (about -0.03 overall, positive inside trends, negative in chop), because a
fixture with strong unconditional momentum would flatter any trend stack and one
with strong mean reversion would condemn it. This one pays only for correctly
identifying which regime is running.

**It still proves nothing about live edge.** What it establishes is that the
machinery is correct, and that the method of composing a stack works. Two
specific cautions:

*Selection shrinkage is real.* The default stack was chosen by looking at the
comparison table above, which makes its in-sample number optimistic. Re-run with
the same config on five seeds never used for selection, 1,042 days:

| Stack | Signals | Accuracy | vs break-even | z |
|---|---|---|---|---|
| trend + persistence + participation | 3,990 | 58.05% | +5.42% | +6.9 |
| trend + persistence | 22,693 | 56.09% | +3.46% | +10.5 |
| trend + thrust + participation | 2,581 | 50.33% | -2.30% | -2.3 |

The chosen stack's edge fell from +7.17% to +5.42%. The ranking held and the
losing variant stayed negative, but the magnitude dropped by a quarter purely
from having selected on the first sample. Expect the same when you move from your
backtest to live, and then expect another haircut for slippage and latency.

*Your payout dominates everything.* A +2.6 point edge is comfortable at 1.95x and
gone at 1.80x. Measure your venue's real fill, not its advertised one.

---

## Going live

The engine will not stop you from doing this badly. In order:

1. **Get real history.** A year of 5-minute bars is about 105,000 bars. Anything
   under a few months cannot distinguish a 2-point edge from noise.
2. **Set your real payout and fees** in the config. Nothing else matters until
   this is right.
3. **Run `gates`, `compare` and `overlap`** on your data. Do not assume the
   default stack transfers. If `momentum_thrust` scores well on your history, use
   it. If `overlap` says a gate adds no information, drop it and re-measure.
   Vary the stake with the signal frequency, or the comparison will lie to you.
4. **Check the calibration table**, not the P&L line. If realised hit rate does
   not rise with conviction, the conviction score is noise and `prob_cap` is a
   fiction.
5. **Hold out the last few months** and never tune against them.
6. **Paper trade against the live feed.** This is where latency, the unclosed
   bar, and the difference between your close and the venue's settlement price
   show up. The `timing` condition exists for exactly these.
7. **Start at a fraction of the sized stake.** Leave `halt_when_unhealthy` on.

Two things this repository does not do, on purpose: it does not place orders, and
it does not tell you a stack is profitable. It tells you what each gate is worth
on your data, and refuses bets that do not clear the payout.

---

## Layout

```
btc5m/
  indicators.py   vectorised primitives (EMA, ATR, RSI, ADX, variance ratio, ...)
  features.py     one pass over the bars -> every factor, no look-ahead
  gates.py        the gate menu; each gate reports its own sub-checks
  signal.py       gate aggregation + the three betting conditions
  risk.py         the three risk conditions, bankroll and exposure state
  backtest.py     bar-by-bar walk with binary settlement and calibration
  attribution.py  what each gate and each stack is actually worth
  redundancy.py   whether the gates and conditions overlap, and by how much
  venue.py        live contract quotes, the window clock, gas and sizing
  data.py         CSV, live exchange fetch, seeded synthetic bars
  config.py       every threshold, validated
  cli.py          python -m btc5m ...
configs/          default, conservative, prediction-market
tests/            259 tests
```

The load-bearing test is `test_a_signal_does_not_change_when_the_future_is_removed`:
it re-evaluates a bar with the rest of the series deleted and asserts the
decision is identical. A second test rewrites future prices instead of deleting
them. Look-ahead bias is what makes short-horizon systems look profitable on
paper and lose money live, so it is tested directly rather than assumed.

```bash
python -m pytest tests/ -q      # 259 passed
```

---

## Configuration

Every threshold is in `config.py` and overridable by JSON, YAML, or `--set`:

```bash
python -m btc5m backtest --data btc_5m.csv \
  --gates data_integrity,volatility_regime,trend_alignment,persistence,participation \
  --set betting.net_payout=0.95 \
  --set betting.required_edge=0.04 \
  --set risk.max_stake_pct=0.01
```

Presets: `default` / `trend5` (the five-gate stack), `trend4` (the same without
`participation`, best risk-adjusted result on the fixture), `core3`,
`trend6-session`, `thrust`, `meanrev`, `everything`. List them with
`python -m btc5m menu`.

Bundled configs: `configs/default.json` (fixed-odds, 1.90x),
`configs/conservative.json` (six gates, tighter risk, halts when unhealthy),
`configs/prediction-market.json` (contract pricing, fees, void on tie),
`configs/trustwallet-bnb-5m.json` (the Trust Wallet BNB Smart Chain market).

Unknown config keys are rejected rather than ignored, so a typo is an error
instead of a silently ignored setting.

---

*Nothing here is financial advice. Five-minute binary bets on BTC are a
negative-sum game before you add an edge; the payout is the house's margin. The
code is built to tell you when it has no edge, and that is the answer you should
expect most of the time.*
