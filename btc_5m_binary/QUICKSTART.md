# Placing your first bets, by hand

This gets you trading today. It places nothing itself: it watches the market,
and when every condition passes it prints the bet in full so you can put it on
the venue yourself, inside the thirty seconds you have.

There is no order-placing code in this repository. That is deliberate for now —
see [the last section](#what-full-automation-still-needs).

---

## 1. On your own machine

Not a server, not a cloud runner: you need to see and hear the alert.

```bash
git clone -b claude/btc-5min-trading-strategy-5gf3z6 \
    https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python.git
cd Algorithmic-Trading-Strategy-Using-Python/btc_5m_binary
python3 -m venv .venv && . .venv/bin/activate
pip install "numpy>=1.24"
python -m pytest tests/ -q          # 536 passed, before you trust it with money
```

## 2. Run it

One command. Copy it as it is.

```bash
python -m btc5m watch --alert \
    --config configs/fade-flow-pooled-5m.json \
    --venue polymarket-btc-5m \
    --set risk.starting_bankroll=1000 \
    --set risk.max_stake_pct=0.0075 \
    --set risk.min_stake=5 \
    --set betting.fee_bps=175 \
    --set betting.tie_policy=favor_up \
    --windows 0 --out quotes.csv
```

What each override is for, because they are not decoration:

| override | why |
|---|---|
| `--venue polymarket-btc-5m` | prices the book at Polymarket's own per-share fee |
| `betting.fee_bps=175` | moves break-even to 51.75%, Polymarket's, not predict.fun's 52.00% |
| `betting.tie_policy=favor_up` | Polymarket resolves Up on a tie; predict.fun splits |
| `risk.starting_bankroll=1000` | your bankroll |
| `risk.max_stake_pct=0.0075` | 0.75%, or $7.50. The smallest size that clears the $5 minimum with the config's own 1.5x headroom |
| `--windows 0` | runs until you stop it with Ctrl-C |

The config file itself is untouched. These are runtime overrides, so the
pre-registered runs it records stay exactly as they were.

## 3. What you will see

Most windows print one quiet line and nothing else. The signal fires on about
one bar in twenty, so expect **a bet every hour or two**, not every window.

When one fires, the terminal bell rings and you get this:

```
  ====================================================================
  BET NOW: DOWN   window 2026-09-16 14:05:00 UTC
  --------------------------------------------------------------------
    buy          DOWN  at  0.44   (limit price)
    stake        $7.00  ->  16 shares
    all-in cost  0.4572 a share, fee included
    model says   56.1%   edge +10.4%
    depth        420 shares at that ask
    entry closes in 25s
    market       https://polymarket.com/event/btc-updown-5m-1789481100
  ====================================================================
```

Open the link, buy that side at that limit, that many shares. If more than
thirty seconds have passed since the window opened, skip it: the edge the
backtest measured assumes you are in early.

## 4. Afterwards

```bash
python -m btc5m settle --quotes quotes.csv
python -m btc5m report --quotes quotes.csv \
    --config configs/fade-flow-pooled-5m.json --venue polymarket-btc-5m
```

`report` tells you whether the side you were sent was the cheap one, which is
the number that decides whether any of this works. Every alert you act on also
adds evidence about which settlement rule the venue uses.

## 5. What you have to do yourself

The venue account is yours to set up and this repository never touches it.

- Fund a Polymarket account with USDC on Polygon.
- Use a wallet that holds **only what you are prepared to lose**.
- **Never paste a seed phrase or private key into this repository, a terminal,
  a chat, or any tool.** Nothing here ever needs one. Anything that asks for one
  is a theft.

## 6. Two things that are still unmeasured

Not reasons to stop, but know them while you place the first bets.

- **The book is about 8.5 points off even when a window opens.** On the cheap
  side you need 43.7% and you have about 53%. On the dear side you need 60.7%.
  Whether the signal lands on the cheap side is not yet known, and `report`
  answers it as your bets accumulate.
- **The settlement rule.** Polymarket resolves on a Chainlink 60-second average,
  and one plausible reading of its wording would put the signal at 49.7%, which
  is no edge at all. Three other readings leave it intact.

Both resolve with a few days of data. Starting small until then is the cheap
version of waiting.

## What full automation still needs

Not built, and each piece is real work:

1. A wallet and API credentials derived from its key, on the machine that trades.
2. EIP-712 order signing against the CLOB, with the venue's tick and size rules.
3. Order lifecycle: placement, partial fills, cancellation when the window closes.
4. Position and bankroll tracking that survives a restart, so the risk limits
   mean something.

Polymarket documents **session keys**: a separate signer with scoped,
time-limited authority over a deposit wallet. That, rather than the wallet's own
key, is the shape this should take when it is built.
