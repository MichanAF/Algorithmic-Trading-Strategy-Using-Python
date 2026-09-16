# Running the quote watcher on a small box

This deploys one thing: a process that reads Polymarket's public API and
Binance's public klines, and appends a CSV row three times a window saying what
the book offered and what the engine thought. It is the measurement described in
[the README](../README.md#the-one-measurement-a-backtest-cannot-make).

**It holds no key, signs nothing, and has no code path that could place an
order.** That is not a policy, it is the current state of the repository: there
is no signer in it. Everything below is therefore a plain read-only service, and
the security posture is correspondingly simple — the box has nothing on it worth
stealing.

Two things this document will not help with. It does not change where you may
use Polymarket: the venue's own terms and the law where you are apply wherever
the server sits, and nothing here is built to route around either. And it does
not make twelve windows into a measurement — the reason to run this on a box at
all is that thousands of windows take weeks, not that a cloud IP is useful.

---

## The instance

| | |
|---|---|
| Region | `me-central-1` (UAE), chosen for proximity and a network you trust |
| Size | the smallest current-generation instance. `t4g.small` (arm64) is ample; numpy publishes aarch64 wheels, so Graviton needs no build tooling |
| Disk | 8 GB. The CSV grows by about 864 rows a day, roughly 40 MB a year |
| Inbound ports | **none.** Not 22 either, if you use SSM Session Manager |
| Outbound | 443 only |
| Price | check the current AWS price list for `me-central-1`; this workload is a few hundred kilobytes of memory and one HTTPS request every few seconds, so the instance is the whole cost |

The load is trivial, and the only resource that matters is the clock.

## The clock, which is the one thing that can silently ruin the data

Every window starts on a five-minute boundary, the entry budget is 30 seconds,
and the watcher records the book at +5, +15 and +30 seconds. A clock ten seconds
slow does not produce an error: it produces a CSV full of readings that claim to
be at +5 seconds and are not. Nothing downstream can detect that.

```bash
sudo dnf install -y chrony || sudo apt-get install -y chrony
sudo systemctl enable --now chronyd
chronyc tracking | grep -E 'System time|Leap status'   # want microseconds, "Normal"
```

Amazon Linux and Ubuntu on EC2 point chrony at the local 169.254.169.123 clock
by default, which is accurate; confirm it rather than assume it. Check
`System time` again after a reboot and after any instance resize.

## Install, without Docker

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin btc5m
sudo install -d -o btc5m -g btc5m /opt/btc5m /var/lib/btc5m

sudo -u btc5m git clone --depth 1 \
    -b claude/btc-5min-trading-strategy-5gf3z6 \
    https://github.com/MichanAF/Algorithmic-Trading-Strategy-Using-Python.git \
    /opt/btc5m/repo
sudo -u btc5m ln -sfn /opt/btc5m/repo/btc_5m_binary /opt/btc5m/app

sudo -u btc5m python3 -m venv /opt/btc5m/venv
sudo -u btc5m /opt/btc5m/venv/bin/pip install --quiet "numpy>=1.24"

# The suite must pass on the box before it is trusted to measure anything.
cd /opt/btc5m/app && sudo -u btc5m /opt/btc5m/venv/bin/python -m pytest tests/ -q
```

Then the units:

```bash
sudo cp /opt/btc5m/app/deploy/btc5m-watch.service   /etc/systemd/system/
sudo cp /opt/btc5m/app/deploy/btc5m-settle.service  /etc/systemd/system/
sudo cp /opt/btc5m/app/deploy/btc5m-settle.timer    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc5m-watch btc5m-settle.timer
journalctl -u btc5m-watch -f
```

## Install, with Docker

```bash
cd /opt/btc5m/app
sudo docker build -f deploy/Dockerfile -t btc5m-watch .
sudo docker run -d --name btc5m-watch --restart unless-stopped \
    -v /var/lib/btc5m:/data btc5m-watch
sudo docker logs -f btc5m-watch
```

Settling, hourly, from cron or a timer:

```bash
sudo docker run --rm -v /var/lib/btc5m:/data btc5m-watch \
    settle --quotes /data/quotes.csv
```

## Check it in the first fifteen minutes

Three windows is enough to catch every way this goes quietly wrong.

```bash
journalctl -u btc5m-watch -n 20 --no-pager
```

A healthy line looks like this, and the two things to read are the book and the
absence of a lag marker:

```
2026-09-15 14:15:00 + 5s  UP 0.50/0.51  DOWN 0.48/0.49  DOWN p 0.541 price 0.508 edge +0.033  bet yes
```

| What you see | What it means |
|---|---|
| `[bar lag 300s]` on every line | the bar feed is a window behind, so the engine is fading the wrong bar. Check which host answered in the first log lines |
| `bars: could not reach ...` | both Binance hosts are blocked. The mirror, `data-api.binance.vision`, is the fallback; no other exchange publishes the taker volume the flow gate needs |
| `no market at that slug` every window | the slug shape has changed. Run `python -m btc5m probe --venue polymarket-btc-5m` and read what came back |
| `a side has no asks` | real, and worth knowing: a window nobody is quoting is a window you could not have traded |

Then confirm the file is growing and the columns are populated:

```bash
wc -l /var/lib/btc5m/quotes.csv
python3 -m btc5m report --quotes /var/lib/btc5m/quotes.csv \
    --config configs/fade-flow-pooled-5m.json
```

`bar lag on 0` in the report's first line is the number that matters.

## Getting the data off the box

The CSV is the only output, and it is append-only, so copying it is safe at any
moment:

```bash
scp btc5m@<host>:/var/lib/btc5m/quotes.csv .
python -m btc5m report --quotes quotes.csv --config configs/fade-flow-pooled-5m.json
```

Nothing here needs the box to be reachable from outside. If you used SSM
Session Manager, `aws ssm start-session` and `aws s3 cp` keep the security group
empty of inbound rules.

## Before any order-placing code exists

None does yet, and the order in which that changes matters more than how:

1. **The quote question has to be answered first.** If the book is not near even
   when the gates fire, the edge measured on Binance closes is not available at
   this venue, and no amount of execution engineering recovers it.
2. **Settlement has to be measured.** Polymarket resolves on Chainlink's BTC/USD
   60-second TWAP at each end of the window; every backtest here settles on
   Binance close-to-close. Those are not the same bet, and the difference is
   measurable from the minute bars already fetched.
3. **Then, and only then, a signer.** In a separate hot wallet holding only what
   you are prepared to lose. The key belongs in a systemd credential, an
   environment variable set outside the repository, or a hardware signer — never
   in git, never in a unit file, never pasted into a terminal that logs. A seed
   phrase controls every asset in the wallet and nothing here ever needs one.
   Polymarket documents **session keys**: a separate signer with scoped,
   time-limited authority over a deposit wallet. That is the right shape for a
   box like this, rather than the wallet's own key.
4. **Latency, measured from this box, not assumed.** The entry budget is 30
   seconds and the round trip is part of it:

   ```bash
   curl -o /dev/null -sS -w 'clob %{time_total}s\n' \
       "https://clob.polymarket.com/midpoint?token_id=<a live token id>"
   ```
