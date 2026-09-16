# Hedged crypto core, built from MetaMask's instrument set

Spot you own, a short perp against it, and **one number that moves** — the hedge
ratio `h`, the fraction of your core whose price exposure is switched off right
now.

```
h = 0.00   fully exposed. You own the core outright.
h = 0.50   half hedged. Half the beta, half the funding.
h = 1.00   delta neutral. No price exposure; funding is the whole return.
```

Everything below is downstream of two facts about the venue, so start there.

---

## Start with the fee schedule, not the market view

| Moving $10,000 of exposure | Cost |
|---|---|
| MetaMask Swaps (spot) | **$97.50** (0.875% per side) |
| Hyperliquid perp, taker | **$3.50** (0.035%) |
| Hyperliquid perp, maker | **−$1.00** (0.01% rebate — it pays you) |

**The perp book is ~28× cheaper than the spot book for moving exposure.** That
single ratio writes the architecture:

> Buy the core once. Never trade it. Manage every exposure decision in the perp.

A strategy that de-risks by selling spot pays 1.95% a round trip to do what
0.07% would have done. Rebalance a core six times a year through Swaps and
you have donated ~11% of it to slippage and fees before the market did anything.

The second fact is the one that hurts:

> **MetaMask Perps is isolated margin only.**

Your spot BTC is *not* collateral for your short BTC perp. They live in
different places — spot in the wallet, perp collateral as USDC on HyperCore. In
a squeeze the short is liquidated on its own merits while the spot leg, up by the
exact move that killed it, sits there unable to help. You end up having realised
the entire hedge loss *and* holding an unhedged book at the top.

That is not a footnote. It is why `sizing.py` derives the split from the rally
the hedge must survive, rather than from anyone's preference.

---

## How long do I hold?

Three different clocks, and conflating them is the most common way to lose money
on this venue.

### The core: indefinitely

At 1.95% a round trip, the core is not a thing you trade. It is a thing you own.
If you want less exposure, you short the perp — you do not sell the spot.

### A carry hedge: days to weeks, and the arithmetic says which

```
 funding APR |  overlay, maker |  overlay, taker |  from cash, taker
--------------------------------------------------------------------
       5.0%  |       immediate |            8.5d |             never
      11.0%  |       immediate |            2.8d |            147.5d
      15.0%  |       immediate |            2.0d |             81.9d
      25.0%  |       immediate |            1.1d |             38.8d
      45.0%  |       immediate |            0.6d |             18.9d
      75.0%  |       immediate |            0.4d |             10.7d
```

Read the two right-hand columns against each other, because they are the same
position reached two different ways:

* **Overlay** — you already own the spot. The hedge's only cost is the perp
  round trip. At baseline funding it has paid for itself in **under 3 days**.
* **From cash** — you buy the spot *in order to* run the trade. Now you have
  added a 1.95% swap round trip, and against the 4% those dollars would have
  earned in mUSD, baseline funding takes **147 days** to break even.

**Same position. Same funding. 3 days versus 147.** That is the whole argument
for running this as an overlay on coins you wanted anyway, and not as a
delta-neutral trade you assemble from stablecoins. On MetaMask, cash-and-carry
from scratch is a four-month commitment at baseline funding — and funding does
not stay put for four months.

Note the maker column. A maker round trip is a *rebate*, not a cost, so a hedge
placed with limit orders is ahead from hour one. Taker fills cost 0.07% round
trip, which is three days of baseline funding. **On short holds, maker versus
taker is the difference between a profit and a loss.**

The default minimum hold is 3× break-even — breaking even is not a reason to do
something; it is the point at which you worked for free.

### A risk hedge: as long as the regime lasts

No minimum. It is insurance, priced against the drawdown it avoids, not against
funding. It comes off when the trend repairs or the volatility shock passes.

---

## Entry

Two independent reasons to put a hedge on, kept independent on purpose.

**Carry entry.** Trailing 24h funding above `enter_apr` (default **15% APR**),
ramping to full size at 45%. The threshold is 15% and not zero because
Hyperliquid's baseline funding is ~11% APR — that is the interest component,
paid to shorts in every regime. Being paid the baseline is not an edge; it is
the weather. You want the *premium* on top of it.

**Risk entry.** Trend break, volatility in its top decile, or core drawdown
approaching the limit. This signal ignores funding entirely.

They combine with `max`, not a sum — **rich funding must never cancel a broken
trend.** Those are the conditions where you want to be hedged twice over, not
netted to flat.

Then three guards stand between the target and the order book:

| Guard | What it stops |
|---|---|
| **Ladder** | Move at most ⅓ of the band per decision. Funding mean-reverts; averaged entries beat precise ones. |
| **Minimum step** | Ignore adjustments under 0.10. Chasing noise at 0.07% a lap is how carry strategies die. |
| **Minimum hold** | Do not close a carry hedge before it paid for itself — *unless* funding flipped, the premise died, or risk took over. |

Practical execution notes: **quote as maker**, and funding is stamped hourly, so
a short opened just before the hour collects that hour in full.

---

## The split

The split is **derived, not chosen.** You pick two things:

1. how much of the core you want hedged at maximum (`max_hedge_ratio`)
2. the rally the hedge must survive with nobody watching (`survive_rally_pct`)

Everything else is arithmetic. Surviving an adverse move `r` on a perp with
maintenance margin `m` takes `r(1+m) + m` of collateral per dollar of hedge
notional — 51.5% to survive +50% on BTC. Multiply by how much you intend to
hedge, and the perp sleeve has claimed its share before the spot sleeve gets a
vote.

```
$25,000, hedging up to 100% of core, surviving a +50% rally, 2x on the short

  bucket                                USD    share  instrument
  ---------------------------- ------------  -------  ----------------------------------
  core spot (held)                   14,818   59.3%  native BTC / ETH / SOL in wallet
    of which staked                   2,223    8.9%  MetaMask pooled staking (illiquid)
  perp collateral (posted)            7,409   29.6%  USDC on HyperCore
  margin reserve (staged)               272    1.1%  USDC on Arbitrum, 1 tx from the perp
  idle cash                           2,500   10.0%  mUSD / Money Account
  ---------------------------- ------------  -------
  total                              25,000  100.0%
```

Change the survivable rally and the whole book moves — which is the point:

| Preset | Survives | Leverage | Core | Perp sleeve | Cash |
|---|---|---|---|---|---|
| conservative | +75% | 1.5x | 50.9% | 39.1% | 10% |
| **balanced** | **+50%** | **2x** | **59.3%** | **30.7%** | **10%** |
| aggressive | +30% | 3x | 68.3% | 21.7% | 10% |

There is no free lunch in that table. A bigger core is bought with a shorter
fuse on the short.

Two details that are easy to get wrong:

* **The reserve lives on Arbitrum, earning nothing.** It is deliberately *not*
  in mUSD. A cross-chain hop during a squeeze is the one transfer that will not
  arrive in time, and 4% APY on a small reserve is not worth a liquidation.
* **Staked ETH still carries full price delta** — the perp hedges it fine — but
  it is illiquid and is never perp collateral. MetaMask also keeps 15% of the
  staking rewards.

---

## Risk management

The margin ladder is the centrepiece, because on an isolated-margin venue the
decision during a squeeze has to be a transfer somebody already sized, not a
judgement call made at speed.

```
margin ladder -- 14,818 short from 95,000, 7,409 posted, 272 staged
  liquidation +48.2% on posted collateral, +50.0% if the reserve lands in time
  rung              move        price               equity      top-up
  watch           +24.1%      117,888        3,839 (  52%)         272
  top up          +33.7%      127,043        2,411 (  33%)         272
  liq (posted)    +48.2%      140,776          269 (   4%)         272
  liq (reserved)  +50.0%      142,500            0 (   0%)         272
```

Two liquidation points, and they are not the same number. The gap between them
*is* the reserve — and it is only real if the reserve is actually staged rather
than merely intended.

The top-up is sized to carry the short **to the planned ceiling**, not to
rebuild a fresh 50% buffer from the new price. A buffer measured from each new
high can never be held; chasing it means topping up forever into a move that is
beating you.

### What leverage really buys

| Leverage | BTC short liquidates at |
|---|---|
| 2x | **+48.5%** |
| 3x | +32.0% |
| 5x | +18.8% |
| 10x | +8.9% |
| 20x | +4.0% |
| 50x | +1.0% |

BTC has done +8.9% in an afternoon. A 10x hedge is not a hedge; it is a short
with a countdown. The default is 2x because an unattended position should
survive a move that has happened before.

### The last resort

When the reserve and idle cash are exhausted and the short is still
under-collateralised, there are three ways out and the fee schedule picks the
winner:

| Option | Cost | Verdict |
|---|---|---|
| Sell core to fund margin | 0.875% | Defends a hedge the market already priced out, at 25× the cost |
| **Trim the short** | **0.035%** | **Fixes the margin ratio directly. This is the default.** |
| Do nothing | the entire posted collateral | — |

After a forced trim there is a **72-hour cooldown**: cuts are still allowed,
rebuilds are not. Without it the policy re-shorts into the same squeeze that
just trimmed it and pays taker fees on every lap. Testing found this doom loop
at ~130 forced trims a year in a strong rally; the cooldown cut trades by ~45%
and forced trims by ~70%.

---

## What the numbers prove, and what they don't

40 synthetic years, median across seeds:

```
strategy                med CAGR  med vol   med DD  med r/v   beat HODL
core only (HODL)          +6.8%    37.0%    32.4%     0.19          --
hedged overlay           +11.4%    18.8%    16.1%     0.61         55%
always delta-neutral      +6.8%     0.2%     1.1%    26.28         52%
```

Now the honesty check. The generator couples funding to price momentum, because
that is how funding actually behaves. Break that coupling (`--beta 0`) and the
carry signal carries no information about price:

```
hedged overlay            +4.4%    21.8%    19.7%     0.22         55%
```

**The return advantage largely evaporates. The risk reduction does not.**

So, plainly:

* **Supported by these numbers:** the overlay roughly halves volatility (37% →
  19–22%) and roughly halves drawdown (32% → 16–20%). That is mechanical —
  carrying less delta means less variance — and it survives decoupling.
* **Not supported:** that it improves returns. +11.4% becomes +4.4% once funding
  stops predicting price, and the overlay beats HODL on CAGR only **55%** of the
  time. That is close enough to a coin flip that you should treat any return
  edge as unproven until you have run it on real funding history.
* **Also found:** in a strong bull market a pinned delta-neutral book **cannot
  stay neutral.** Margin pressure force-trims it — ~18 times a year — and the
  venue quietly converts it into a reluctant long. "Delta neutral" is a
  description of an intent, not a guarantee.
* **A known weakness:** in a choppy uptrend the overlay whipsaws and can
  materially underperform HODL. One seed returns −6.8% in a +57% year.

The synthetic generator is a testbed, not evidence. Its funding averages +7% to
+13% APR with ~35% of hours negative, which is plausible but is not your
exchange's history. Point it at real hourly price and funding data before you
believe any of it.

---

## Quick start

```bash
pip install numpy

python -m mmhedge venue                          # what the venue charges
python -m mmhedge plan --capital 25000           # the split, derived
python -m mmhedge carry --funding 0.25           # how long to hold
python -m mmhedge hedge --funding 0.30 --trend -0.4
python -m mmhedge risk --capital 25000 --hedge 1.0 --price 95000
python -m mmhedge compare --synthetic 8760       # overlay vs HODL vs neutral
python -m mmhedge sweep --seeds 40 --beta 0      # how much was the coupling?
```

With real data — hourly `timestamp,price,funding` (or `funding_apr`):

```bash
python -m mmhedge compare --data btc_hourly.csv --config configs/balanced.json
```

From Python:

```python
from mmhedge import config_from_dict, allocate, decide, MarketState, quote_carry

cfg = config_from_dict({"capital_usd": 25_000})
print(allocate(cfg).report())
print(decide(MarketState(funding_apr=0.30, trend_score=-0.4, core_usd=15_000), cfg).report())
print(quote_carry(cfg.venue, 0.30, leverage=2.0).report())
```

Every decision reports the condition that drove it, so a refused hedge or a
margin call always names the exact rule that fired.

---

## The MetaMask instrument map

What the strategy uses, and what it costs (verified September 2026 — **re-check
before sizing anything real**, these are numbers someone else can change on a
Tuesday):

| Instrument | Role | Cost / yield |
|---|---|---|
| Native BTC, ETH, SOL, EVM tokens | the core | 0.875% per swap |
| MetaMask Perps (Hyperliquid) | the overlay | 0.035% taker / −0.01% maker, hourly funding, **isolated margin**, up to 50x |
| USDC on HyperCore | perp collateral | — |
| USDC on Arbitrum | staged reserve | $1 withdrawal, $10 minimum funding |
| mUSD / Money Account | idle cash | up to 4% variable APY |
| Pooled ETH staking | core yield | MetaMask keeps 15% of rewards |

Perps also list US equities, commodities and FX. Those are in the tier table as
`non_crypto` and explicitly flagged **unverified** — the margin bracket there is
a conservative guess, not a published number. Check it in the app before sizing
against it.

---

## Layout

| File | What it decides |
|---|---|
| `venue.py` | what MetaMask and Hyperliquid charge; margin tiers; the funding formula |
| `carry.py` | break-even and minimum hold — the "how long?" arithmetic |
| `sizing.py` | the split, solved from the survivable rally |
| `hedge.py` | the hedge ratio: two signals, three guards |
| `risk.py` | risk conditions and the margin ladder |
| `backtest.py` | hour-by-hour, paying every fee |
| `data.py` | hourly price *and* funding, correlated |
| `configs/` | conservative / balanced / aggressive |

```bash
python -m pytest tests/ -q        # 134 tests
```

---

## This is not financial advice

It is a model of one venue's cost structure and a policy defined on top of it.
The numbers it prints are only as good as the fees, funding and margin brackets
you feed it. Hedging removes upside as reliably as it removes downside; a
delta-neutral book earns funding and nothing else, and funding goes negative.
Isolated margin means a hedge can be liquidated by a move your core is profiting
from. Decide your own risk.
