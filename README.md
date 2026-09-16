# Algorithmic-Trading-Strategy-Using-Python

Algorithmic Trading Using Python

## Strategies

### Dual moving average crossover (stocks)

`Algorithmic Trading Code` — a Colab notebook script that signals buy and sell on
a 30/100-day simple moving average crossover, demonstrated on Draft Kings
(`DKNG.csv`).

### BTC 5-minute binary up/down — [`btc_5m_binary/`](btc_5m_binary/)

A gated strategy for a single yes/no question: over the next five minutes, is BTC
up or down? Three to five gates must each pass before the strategy will form an
opinion, three betting conditions decide whether that opinion is worth money at
the offered payout, and three risk conditions size the stake or refuse it.

```bash
cd btc_5m_binary
python -m btc5m signal   --synthetic 20000      # full gate trace for one bar
python -m btc5m gates    --data btc_5m.csv      # what is each gate worth?
python -m btc5m compare  --data btc_5m.csv      # which gate stack should I run?
python -m btc5m backtest --data btc_5m.csv
```

See [`btc_5m_binary/README.md`](btc_5m_binary/README.md) for the gate menu, the
payout arithmetic that sets the bar, and what the bundled numbers do and do not
prove.

### Hedged crypto core on MetaMask — [`metamask_hedge/`](metamask_hedge/)

Spot you own, a short perp against it, and one number that moves: the hedge
ratio. Built around two facts about the venue — MetaMask Swaps cost 0.875% while
Hyperliquid perps cost 0.035%, so the core is bought once and every exposure
decision happens in the perp; and MetaMask Perps is isolated margin only, so a
rally can liquidate the short that was hedging a core profiting from the same
move.

```bash
cd metamask_hedge
python -m mmhedge venue                     # what the venue charges
python -m mmhedge plan  --capital 25000     # the split, derived not chosen
python -m mmhedge carry --funding 0.25      # how long a hedge must be held
python -m mmhedge risk  --capital 25000 --hedge 1.0 --price 95000
python -m mmhedge sweep --seeds 40 --beta 0 # how much of the edge was real?
```

See [`metamask_hedge/README.md`](metamask_hedge/README.md) for the hold-duration
arithmetic, the margin ladder, and the decoupling test that separates the
strategy's risk reduction (robust) from its return edge (not).
