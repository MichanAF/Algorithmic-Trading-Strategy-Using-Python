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

## The gate stack: three gates, three different questions

Gates come in three kinds, and they run in this order:

1. **Veto gates** have no directional opinion. They decide whether the market is
   fit to bet on at all. One failure kills the bar, cheaply.
2. **Directional gates** vote UP or DOWN and carry a 0-1 score. They must agree.
3. **Confirmation gates** are told the side the directional gates chose and may
   only veto it, never propose their own.

The default stack is **three gates**, one per genuinely distinct question:

| Gate | Kind | The question | Why this one |
|---|---|---|---|
| `data_integrity` | veto | Is the input real? | A gapped bar, stuck price or blown spread turns the bet into a coin flip at worse odds. Costs nothing to check. |
| `trend_alignment` | directional | Which way? | The only gate whose removal drops the stack below break-even. Strongest on its own (53.5%, z = +4.3) and in combination (+3.5 points marginal, z = +4.1). |
| `persistence` | directional | Does "which way" mean anything right now? | Five-minute BTC returns are mildly negatively autocorrelated, so following a trend only pays in the subset of time when moves extend. The variance ratio is that test. +2.2 points marginal, z = +3.1. |

Both directional gates earn their place: `btc5m overlap` confirms each still
separates winners from losers once the other has agreed. Nothing in this stack
is dead weight, which is the point of picking three rather than five.

Every gate reports its own sub-checks, so a refused bar names the exact
condition that stopped it rather than reporting a bare verdict.

**One honest caveat.** `trend_alignment` and `persistence` fire on largely
independent bars (phi +0.06) but agree on *direction* 98% of the time. So read
this stack as one direction opinion plus one regime filter plus one integrity
check — not as two independent votes on which way. If you would rather that be
explicit, swap `persistence` for the `regime` gate, which runs the same variance
ratio and ADX test as a veto with no direction vote. It measured equal
(55.8% vs 56.3% out of sample, within noise).

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

**`volatility_regime` does not belong on a yes/no contract.** Its floor exists
because on spot or perps a move smaller than the spread you cross loses money
even when the direction was right. A binary bet crosses no spread: you pay a
contract price, and any non-zero move resolves it. Measured over 1,042 unseen
days, accuracy is flat across the entire volatility range:

| ATR percentile | Signals | Accuracy |
|---|---|---|
| below the 0.30 floor | 689 | 57.9% |
| inside the 0.30–0.92 band | 1,699 | 58.2% |
| above the 0.92 ceiling | 496 | 57.1% |

Standard errors are around 2.5%, so those are the same number. Removing the gate
doubled signal frequency and slightly raised accuracy. Extreme volatility is
still worth standing down for, but that is a tail guard and it already lives in
the risk layer as `atr_shock_rank`.

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

## Cherry-picking three gates

Ten viable three-gate combinations, scored on 1,042 days never used for
selection. `EV/day` is expected profit per day in units of one stake at a 0.50
quote — the objective that matters, because accuracy alone does not pay:

| Three-gate stack | Signals/day | Accuracy | EV/day | Survives a price up to |
|---|---|---|---|---|
| **integrity + trend + persistence** | 44.8 | **56.28%** | **5.64** | 0.56 |
| integrity + regime + trend | 48.7 | 55.77% | 5.62 | 0.56 |
| session + trend + persistence | 43.9 | 56.32% | 5.55 | 0.56 |
| integrity + regime + persistence | 44.6 | 55.19% | 4.63 | 0.55 |
| integrity + trend + location | 43.3 | 53.14% | 2.72 | 0.53 |
| volatility + trend + persistence | 21.8 | 56.09% | 2.65 | 0.56 |
| integrity + trend + participation | 15.0 | 55.24% | 1.57 | 0.55 |
| regime + trend + participation | 7.3 | 57.36% | 1.07 | 0.57 |
| integrity + persist + participation | 7.5 | 53.68% | 0.55 | 0.54 |
| integrity + trend + thrust | 9.4 | 50.84% | 0.16 | 0.51 |

The pick was made from purpose first — integrity, direction, regime — and the
measurement agreed. The top two are a statistical tie, so the tiebreak is
design: `persistence` keeps a +DI/-DI agreement filter that `regime` drops.

### What is deliberately left out

| Gate | Why not |
|---|---|
| `volatility_regime` | Its floor assumes a spread you cross. A yes/no contract crosses none — any non-zero move resolves it. Accuracy is flat across the whole volatility range. |
| `participation` | Its verdict adds no information once the other two agree (z = +1.9). Costs 80% of signals for ~1.5 points. |
| `momentum_thrust` | Anti-predictive over the very next bar (z = −8.2). A sharp three-bar push is more often followed by a give-back. |
| `location` | Harmful as a voter on a trend stack; a trending market is supposed to be near its recent extreme. |
| `session` | Roughly free, but spends a slot on a veto with no directional content. Add it as a fourth if you trade through funding times. |
| `mean_reversion` | Contradicts `persistence` by construction, and unproven on its own. |
| `cross_asset` | Needs a second series wired up. Worth testing if you have ETH data. |

### Frequency is set by conviction, not by gate count

Three gates fire *often* — 44.8 times a day at the default floor. With the gate
count fixed, `min_conviction` is the throttle:

| `min_conviction` | Signals/day | Accuracy | EV/day | Stake for 4%/day risk |
|---|---|---|---|---|
| 0.00 | 54.9 | 55.51% | 6.06 | 0.073% |
| 0.60 | 44.8 | 56.28% | 5.64 | 0.089% |
| 0.70 | 36.3 | 57.03% | 5.11 | 0.110% |
| 0.80 | 22.9 | 58.07% | 3.69 | 0.175% |
| 0.85 | 16.2 | 58.87% | 2.88 | 0.247% |
| 0.90 | 10.9 | 59.73% | 2.11 | 0.369% |

Raw expected value is highest at the *lowest* floor. Two things push back:

**Fixed costs per bet.** Both venues charge a proportional fee, which frequency
does not change: staking twice as often at half the size costs the same. Gas is
different — it is a flat charge per bet, so halving the bet count halves it. That
is the whole reason the predict.fun config runs a 0.85 floor and the Polymarket
config runs 0.70: BNB Chain gas makes frequency expensive, and Polymarket's
gas-free CLOB makes it nearly free.

**Venue minimum orders.** Higher accuracy means a bigger per-bet stake for the
same daily risk, which is what clears a 5 USDC minimum on a smaller bankroll.

## Overlap analysis: are the gates asking different questions?

A stack is only as selective as the number of **independent** questions it asks.
Two gates that fire together for the same underlying reason do not double the
evidence. They halve the signal count while adding nothing, and make the stack
look more confirmed than it is.

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

On the three-gate stack:

| Gate | Marginal separation | z | Verdict |
|---|---|---|---|
| `trend_alignment` | +3.49% | +4.1 | adds information |
| `persistence` | +2.20% | +3.1 | adds information |

Feature sets are disjoint, and both gates still pay once the other has agreed.
That is what cherry-picking is supposed to produce. It is also why the stack
stops at three: the audit found nothing left to cut.

### What the audit found on the way here, and what changed

| Overlap | Finding | Action |
|---|---|---|
| `trend_alignment` vs `persistence` | Independent on *when* they fire (phi +0.06) but **98% agreement on direction** | Documented: one direction opinion plus one regime filter, not two votes. `regime` exists as the explicit single-vote alternative. |
| `participation` marginal value | **z = +1.9** — no information once the others agreed | Cut from the stack. |
| `volatility_regime` | Accuracy flat across its whole band on a contract bet | Cut. Its floor assumes a spread this bet never crosses. |
| Betting conditions 1 and 2 | `priced_edge` **never binds** at the default payout | `effective_conviction_floor()` names the single real threshold. |
| Betting condition 3 | `timing` is **not testable** on historical bars | Reported as such rather than counted as passing. |
| Kelly vs stake cap | The cap binds at every conviction, so `kelly_fraction` never sizes a bet | The stake check now names which half bound. |
| `daily_loss_limit` vs `max_drawdown` | The halt flag made **both** fail together | Fixed: the halt belongs to `max_drawdown` alone. |

### Two bugs the venue work exposed

Both were mine, both silent, and both would have cost real money:

**The health halt deadlocked.** `halt_when_unhealthy` stopped betting when the
rolling hit rate dipped — but the rolling hit rate can only improve by placing
more bets, so it locked out permanently while each refusal read like an ordinary
risk decision. It blocked 95% of signals in a run that looked like it was
working. The halt is now sticky *and named*, so it shows up as HALTED instead of
hiding; the venue configs use the derate path, where size falls and the
measurement keeps refreshing.

**A stake cap sitting on the minimum stake stops the strategy for good.** Set
`max_stake_pct × bankroll` equal to `min_stake` and the first loss drops the cap
below the floor, after which every bet is refused forever. The first
predict.fun config did exactly this and placed **one bet in a year**. The
validator now rejects any config without 1.5x headroom, and says what to change.

### The trap this analysis walked into first

Comparing stacks by accuracy alone reverses the answer, and so does comparing
them by raw profit. At a **fixed** stake, loose stacks all trip the drawdown
halt within days, because 43 bets a day at 2% risks most of the bankroll daily:

| Stack | Bets placed | Net P&L | Halted on |
|---|---|---|---|
| 5 gates | 730 | +36,611 | day 204 of 365 |
| 4 gates | 297 | +6,196 | day 24 |
| 3 gates | 148 | +1,355 | day 9 |

Read at face value that says selectivity wins by a mile. It does not — those
numbers measure how fast each stack hit a risk limit calibrated for four bets a
day. **Stake and signal frequency have to be varied together.** That is an
overlap between the betting layer and the risk layer, and it is the most
expensive one here: it inverts the conclusion.

### Read `z`, not the headline

The `verdict` column is deliberately conservative. Under 100 joint passes, or
under 50 bars in either bucket, it reports "too few to judge" rather than a
number. A gate that looks brilliant on 57 signals in a year has told you nothing.

## One-year backtest

The three-gate stack with each venue's own risk settings, 105,120 bars
(365 days) of the bundled fixture at seed 11:

| | predict.fun | Polymarket |
|---|---|---|
| Conviction floor | 0.85 | 0.70 |
| Bets placed | 4,259 (11.7/day) | 8,935 (24.5/day) |
| Hit rate | 58.11% | 56.97% |
| Break-even to beat | 52.00% (200 bps) | 51.75% (taker fee) |
| Edge over break-even | +6.11% | +5.22% |
| Max drawdown | 4.41% | 2.72% |
| Annualised Sharpe | 8.09 | 9.96 |
| Halted | no | no |

```bash
python -m btc5m backtest --config configs/predict-fun-bnb-5m.json \
  --synthetic 105120 --seed 11
```

**The returns those runs produce are not forecasts and are not quoted here.**
The data is synthetic, the stack was selected on it, and Sharpe near 10 on a
five-minute coin flip should read as a warning about the fixture, not a
promise. The transferable numbers are the hit rate against break-even and the
drawdown, and even those need re-measuring on real history.

### Conviction now predicts accuracy, which it did not before

The five-gate stack had a **non-monotone** calibration: its most confident
bucket was its only losing one, 16 points below what the model claimed. The
three-gate stack does not:

| Conviction bucket | Bets | Realised |
|---|---|---|
| 0.70 - 0.78 | 2,647 | 55.7% |
| 0.78 - 0.85 | 2,842 | 56.4% |
| 0.85 - 0.93 | 1,747 | 57.0% |
| 0.93 - 1.00 | 1,699 | 59.8% |

Monotone and rising. The likely reason the old stack inverted: with five gates,
maximum conviction meant every gate maximally extended at once, which is a
late-stage trend. Fewer gates, less of that artefact.

Check this table on your own data before trusting `prob_cap`. If realised
accuracy does not rise with conviction, the conviction score is noise.

## Which venue: predict.fun, Polymarket, or Hyperliquid

Checked September 2026. **Verify before committing money** — these are
fast-moving products and the fee and resolution details are what decide whether
an edge survives.

| | predict.fun | Polymarket | Hyperliquid |
|---|---|---|---|
| 5-minute BTC up/down? | **yes** | **yes** | **no** |
| Horizon | 5 min | 5 min | daily, settles 06:00 UTC |
| Shape | up/down from window open | up/down from window open | above a **strike** |
| Chain | BNB Smart Chain | Polygon | Hyperliquid L1 |
| Collateral | USDT | USDC | USDH |
| Settles from | **per market** — a live one declares Pyth BTC/USD | Chainlink BTC/USD | HyperCore mark price |
| Exact tie | pays 0.50 to both sides | resolves **Up** (`>=`) | n/a |
| Taker fee | `feeRateBps` — **200** on a live market | `shares × 0.07 × p × (1−p)` | zero (initial testing) |
| Maker fee | not separately documented | **zero** | zero |
| Min order | not documented | 5 USDC | n/a |
| Per-bet gas | yes, BNB Chain | none (off-chain CLOB) | none |
| API | `api.predict.fun`, `x-api-key`, Python and TS SDKs | Gamma + CLOB + WebSocket | Python SDK |

**Hyperliquid is the wrong shape.** HIP-4 outcome markets launched on mainnet in
May 2026, but the recurring BTC binary is *daily* and asks whether the mark
price clears a **strike** at 06:00 UTC. That is a dated option, not a coin-flip
direction bet, and nothing in this repository models it. Fees are currently zero,
which is attractive — but you would be writing a different strategy.

**The market in Trust Wallet already is predict.fun.** Trust Wallet's
Predictions tab and Binance Wallet's prediction markets both surface predict.fun
on BNB Chain, so there is no integration to choose between: it is the venue
you are already on, and it has a documented API.

**Polymarket's fee is the detail that matters.** `0.07 × p × (1−p)` per share
peaks at `p = 0.5` — exactly where a five-minute direction strategy wants to
trade. At a 0.50 quote that is 1.75 cents a share, 3.5% of notional, on every
bet:

| Quote | Fee per share | Effective price | Fee as % of notional |
|---|---|---|---|
| 0.30 | 0.0147 | 0.3147 | 4.90% |
| 0.45 | 0.0173 | 0.4673 | 3.85% |
| **0.50** | **0.0175** | **0.5175** | **3.50%** |
| 0.60 | 0.0168 | 0.6168 | 2.80% |

The **maker fee is zero**. Posting a limit order instead of taking removes the
cost entirely, and that is the single biggest execution lever on this venue —
paid for in fill risk, which a 45-second entry window makes real.

### On cost, Polymarket wins at every stake

This conclusion **reversed** once predict.fun's real fee was read off a live
market. An earlier version of this table modelled predict.fun as fee-free and
found a crossover near an $8 stake, below which Polymarket's proportional fee
beat BNB gas and above which predict.fun won. There is no crossover. A live
`CRYPTO_UP_DOWN` market carries `feeRateBps: 200`, and 2 cents a contract is
**dearer than Polymarket's taker fee at every price**: `0.07 × p × (1−p)` peaks
at 1.75 cents, at `p = 0.50`. predict.fun then pays gas on top.

At a 56.28% hit rate and a 0.50 quote, profit per bet:

| Stake | predict.fun (200 bps + $0.30 gas) | Polymarket taker | Polymarket maker |
|---|---|---|---|
| $5 | +0.11 | +0.44 | +0.63 |
| $10 | +0.52 | +0.88 | +1.26 |
| $20 | +1.35 | +1.75 | +2.51 |
| $100 | +7.93 | +8.75 | +12.56 |

Per dollar staked: +8.23% on predict.fun *before* gas, +8.75% as a Polymarket
taker, +12.56% as a Polymarket maker.

**One caveat, and it cuts predict.fun's way.** What the 200 bps is charged *on*
is not documented. Everything here reads it as basis points of the contract's
1.00 face value — 2 cents a contract whatever you paid — which is the
pessimistic reading and the same convention `fee_bps` uses everywhere else in
this repository. If it is charged on the premium instead, it is 200 bps of 0.50
= 1 cent at an even quote, and predict.fun becomes the cheaper taker. **One real
fill settles it.** Read the rate per market with `PredictMarket.fee_rate_bps`
rather than trusting the profile default.

### Bankroll, because 3 gates fire often

Holding total daily risk to 4% of bankroll:

| Config | Bets/day | Stake | Needs a bankroll of |
|---|---|---|---|
| predict.fun, floor 0.85 | 16.2 | 0.25% | ~$5,000 |
| Polymarket, floor 0.70 | 36.3 | 0.11% | ~$12,000 |

Polymarket's 5 USDC minimum is the binding constraint: 0.11% of bankroll has to
clear it with headroom. Run it on much less and the stake cap lands on the
minimum, which stops the strategy on the first loss — the validator now refuses
that config rather than letting you discover it in a month.

### The window clock will cost you money before the fees do

All three venues trade continuously across the window, so the quote drifts from
roughly even at the open toward the realised answer at the close. A market 253
seconds in sitting at 84/16 is not an opportunity; it is the market telling you
the move already happened while your signal went stale.

Our edge exists at **one moment**: the bar close that opens the window. Three
vetoes enforce that:

| Veto | Default | Why |
|---|---|---|
| `entry_window` | within 45s of the open | After that, part of the move you are betting on is already history |
| `time_to_expiry` | at least 60s left | Below that you are betting on the tail of the window |
| `market_not_decided` | quote skew ≤ 0.12 | A lopsided quote means the market knows something the signal does not |

```bash
python -m btc5m quote --config configs/polymarket-5m.json --down 49 --brief
DOWN - buy at 0.507 - stake 13.00 - edge +0.113 - 294s left
NO BET - market already decided (skew 0.34)
NO BET - price 0.636 too high for p 0.621
NO BET - persistence: returns_trend
```

The price shown includes the venue's fee, so the same 0.49 quote reads as 0.507
on Polymarket and 0.490 on predict.fun. Each config declares its own venue, so a
Polymarket config cannot be priced with the wrong fee schedule by accident.

The opposite side is reported but deliberately not taken. When the quote is
lopsided the other side looks cheap, and buying it means betting against your
own signal on the strength of a price that moved because you were late.

### Break-even is the price you pay

Our probability model caps at `prob_cap` (0.64), so there is a hard ceiling on
what it can ever justify buying: **nothing above about 0.60**, and on Polymarket
nothing above about 0.58 once the fee is added. A model claiming 85% confidence
in a five-minute BTC direction is lying, so this is a feature.

### Price source: read it per market, do not assume it

Settlement uses an oracle mid-price, not the last traded price. The oracle is
**not** a venue-wide constant on predict.fun, and this is a live contradiction
worth knowing about:

| Source | Says predict.fun settles on |
|---|---|
| Trust Wallet's own rules text | Chainlink **BTC/USDT** top-of-book |
| A live `CRYPTO_UP_DOWN` market's `variantData` | **Pyth BTC/USD** |

The market is the authority. `PredictMarket.feed` returns what the market you
are about to bet on actually declares — provider, symbol, feed id, and the
window's start and end prices once it settles — and `PREDICT_FUN_BTC_5M.price_feed`
is only a backtest default. Polymarket settles on Chainlink BTC/USD.

For backtesting the choice is immaterial: Binance's BTCUSDT spread is about a
cent against a median five-minute move of about 32 dollars, and only 0.008% of
bars move less than half a spread. For a live bet it is not immaterial — a
five-minute window can be decided by less than the gap between two feeds, and on
this bet the feed *is* the settlement rule.

### Latency, which this strategy is unusually sensitive to

predict.fun's primary servers are in **ap-northeast-1 (Tokyo)**. The entry
window is 45 seconds wide, so where you run from is not a detail: from Japan or
nearby the round trip is tens of milliseconds, from Europe or the US east coast
it is a couple of hundred plus BNB Chain block time. Measure yours before
trusting the `entry_window` default.

### predict.fun accounts: EOA or Smart Wallet

Two ways to reach the protocol, and they need different things:

| | What you need |
|---|---|
| **EOA** | an ordinary wallet private key |
| **Smart Wallet** ("Predict Account") | the account address (your deposit address) **and** the Privy wallet private key |

The web app creates a Smart Wallet for you automatically, so if you have been
betting through Trust Wallet you are on that path. The Privy key is exported
from `predict.fun/account/settings`.

Their own SDKs handle the signing: `pip install predict-sdk`
([source](https://github.com/PredictDotFun/sdk-python)), or the TypeScript one.
Both are published on Context7, so an assistant with network access can read
them directly.

**Keep the key out of this repository.** Read-side calls need only the API key,
and nothing here asks for more than that.

### One-time setup: token approvals

Before any order will go through, the wallet has to approve the protocol's
contracts on-chain. Their SDK does it:

```
setApprovals()                                                    # everything, one call
getApprovalSteps({operation: "TRADE", isNegRisk, isYieldBearing})  # or getAllApprovalSteps()
runApprovals(steps, {skipSatisfied: true, stopOnError: true, onProgress})
```

`setApprovals()` does the lot in one call and is the simplest path. The scoped
API is for building an approval UI. `isNegRisk` and `isYieldBearing` describe
the market and come from `GET /markets`, so `predictfun.py` captures both on
every market it reads.

Each step reports `checking -> skipped | submitting -> confirmed | failed`, and
the result is `{success, steps}`. `skipSatisfied` means re-running is cheap: it
pre-checks on-chain and sends nothing for approvals already in place.

Two things worth knowing. `getApprovalSteps` and `getAllApprovalSteps` do not
touch the chain, so they run without a signer and can render the checklist
before a wallet is connected. Everything after that (`checkApprovals`,
`setApproval`, `runApprovals`) needs one.

**Do this once, manually, before wiring anything automated.** An unapproved
wallet fails at the last step of a five-minute window, which is the worst place
to discover it.

What gets approved: ERC-1155 (`ConditionalTokens`) and ERC-20 (`USDT`), against
both `CTF_EXCHANGE` and `NEG_RISK_CTF_EXCHANGE`. The signing wallet **must be
the order's `maker`**.

### Set the RPC polling interval, or lose four seconds a transaction

ethers defaults `provider.pollingInterval` to **4000ms**, so every internal
`tx.wait()` can take up to four seconds to notice a transaction that already
mined. Against a 45-second entry window on a chain with ~3-second blocks, that
is most of the budget spent waiting for a poll.

```js
provider.pollingInterval = 300;   // BNB is fast enough to justify it
```

Set it on whatever provider you pass in, browser wallets included. Combined with
predict.fun's Tokyo servers this is the difference between comfortably inside
the window and missing it.

### The API shape, confirmed

Field names come from a live testnet response, not from guessing:

| | |
|---|---|
| Envelope | `{success, data, cursor}` |
| Market id | `id`, an integer, plus `conditionId` |
| Title | `question` (`title` is a short label) |
| Kind | `marketVariant` — `CRYPTO_UP_DOWN` or `DEFAULT` |
| Live filter | `tradingStatus == "OPEN"` |
| Order flags | `isNegRisk`, `isYieldBearing` |
| Fee | `feeRateBps` — 200 on a live crypto market |
| Outcome token | `onChainId` |
| Prices | `bestBid` / `bestAsk`, each `{price, size}`, **null when resolved** |
| Window | not published — derived, see below |
| Settlement feed | `variantData.priceFeedProvider` / `priceFeedSymbol` / `priceFeedId` |
| Settled prices | `variantData.startPrice` / `endPrice` |

**Prices are a book, not a number.** You pay the **ask**, so that is what the
strategy prices against; the mid is the fairer read of what the market believes
and is what the skew veto uses. Pricing the edge against the mid would overstate
it by half the spread on every bet.

`status` is not sent: it is a server-side enum and `ACTIVE` is not one of its
values, which testnet answers with a 400. Observed: `REGISTERED` then `RESOLVED`
for `status`, `OPEN` then `CLOSED` for `tradingStatus`.

#### The window has to be derived, because nothing publishes it

This was the last real unknown, and the answer is not where it was expected to
be. A `CRYPTO_UP_DOWN` market states its window **nowhere machine-readable**:

```json
"variantData": {"type": "CRYPTO_UP_DOWN",
  "priceFeedProvider": "PYTH", "priceFeedSymbol": "BTC_USD",
  "priceFeedId": "0xe62df6c8…a415b43",
  "startPrice": 67975.85, "endPrice": 67203.16801979}
```

Prices and a feed, no times. There is no `startsAt` at the top level either. The
two machine-readable pieces are `categorySlug`, which ends in the duration
(`btc-usd-up-down-2026-02-11-09-30-15-minutes`), and `createdAt`, which lands a
few seconds inside the window it opens. Flooring `createdAt` to the duration grid
recovers the boundary exactly:

```python
seconds = duration_from(slug)                 # 900
start   = (created_at // seconds) * seconds   # exact window open
```

**Why not parse the title?** `"Bitcoin Up or Down - September 12, 8:15AM-8:20AM
ET"` states the window in Eastern Time, which means a DST rule and a locale in
the hot path of a 45-second entry window. The slug and `createdAt` are both UTC
and both exact. `_window()` still prefers explicit `startsAt`/`endsAt` if a
market kind ever publishes them.

#### Both window lengths exist, so the filter matters

The market that confirmed this shape was a **15-minute** one — slug `-15-minutes`,
window 900 seconds. predict.fun lists **5-minute** BTC windows too, which is what
the Trust Wallet app shows ("Bitcoin Up or Down — 8:15AM-8:20AM ET") and what
this strategy is built for. A 5-minute market's slug reads `-5-minutes` and
derives a 300-second window by the same arithmetic.

That both lengths are listed side by side is the reason
`btc_five_minute_markets()` filters on `window_seconds == 300` rather than on the
title. The two look nearly identical in a listing, and a 5-minute signal on a
15-minute window is a different bet at the same price: three times the horizon,
so the edge the gates measured over one bar is diluted across three. An unfiltered
`markets()` call will hand you both.

### What is not built: signing and sending

The engine decides. It does not place orders, and the gap is real:

- Both venues need a funded wallet and either a signed on-chain transaction
  (predict.fun on BNB Chain) or an authenticated CLOB order (Polymarket).
- **Never paste a seed phrase or private key into this repository, a terminal,
  a chat, or any tool.** A recovery phrase controls every asset in the wallet,
  not a trading balance. Anything that asks for it is a theft.
- Automate from a **separate hot wallet** holding only what you can lose, with
  its key in an environment variable or a hardware signer, never in git.
- Latency matters. The entry window is 45 seconds; measure the full round trip
  before trusting that default.

The honest next step is to run `quote` against live markets and place bets by
hand for a few weeks. It costs nothing, and it tests the assumption most likely
to be wrong: whether the quote is ever near even at the moment the signal fires.
If it never is, the edge does not exist at that venue however good the gates are.

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
configs/          default, conservative, prediction-market,
                  predict-fun-bnb-5m, polymarket-5m
tests/            326 tests
```

The load-bearing test is `test_a_signal_does_not_change_when_the_future_is_removed`:
it re-evaluates a bar with the rest of the series deleted and asserts the
decision is identical. A second test rewrites future prices instead of deleting
them. Look-ahead bias is what makes short-horizon systems look profitable on
paper and lose money live, so it is tested directly rather than assumed.

```bash
python -m pytest tests/ -q      # 326 passed
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

Bundled configs: **`configs/predict-fun-bnb-5m.json`** (what Trust Wallet
surfaces) and **`configs/polymarket-5m.json`** are the two to use.
`configs/default.json` (fixed odds, 1.90x), `configs/conservative.json` and
`configs/prediction-market.json` are kept as references for the non-contract
case; note the latter two still run larger stacks, so numbers quoted for them
elsewhere in this README were measured with those gates.

Unknown config keys are rejected rather than ignored, so a typo is an error
instead of a silently ignored setting.

---

*Nothing here is financial advice. Five-minute binary bets on BTC are a
negative-sum game before you add an edge; the payout is the house's margin. The
code is built to tell you when it has no edge, and that is the answer you should
expect most of the time.*
