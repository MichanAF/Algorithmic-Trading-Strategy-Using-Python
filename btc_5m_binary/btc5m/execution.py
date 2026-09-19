"""Submitting orders, and the rails that keep a bug from emptying the wallet.

The engine decides; this places. It is the only part of the package that can
lose money, so it is built rails-first: every guard below refuses by default and
has to be switched on deliberately.

    OrderTicket   what we intend to buy, in shares, at a limit
    Guard         the refusals: armed, size, day loss, one per window, kill file
    DryRunBroker  logs a ticket and places nothing.  The default
    ClobBroker    the real thing, through Polymarket's own client

Four things this deliberately cannot do:

* **Sell.**  The strategy buys a side and holds it to settlement, so there is no
  exit path and no code here that could build one.  A SELL ticket is refused.
* **Rest an order.**  Orders go in as fill-and-kill: take what is at the limit
  now, cancel the remainder.  A resting order outliving its five-minute window
  is a position nobody decided to hold.
* **Hold a key.**  The private key is read from an environment variable that
  systemd fills from a credential file.  It is never written, logged, printed,
  or passed to anything but the signer.
* **Exceed what you declared.**  A balance ceiling, a per-order cap and a daily
  loss cap are enforced here, in this process, independently of the venue.

The response shapes of ``post_order`` are recorded raw in the journal rather
than trusted: the first live order is the thing that confirms them, and until
then a missing field must read as "unknown", never as "filled".
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

CHAIN_POLYGON = 137
CLOB_HOST = "https://clob.polymarket.com"
KEY_ENV = "POLYMARKET_PRIVATE_KEY"          # set by systemd from a credential
KILL_FILE = "STOP_TRADING"

# Fill-and-kill: take what is resting at the limit, cancel the rest.  Never GTC.
ORDER_TYPE = "FAK"


class ExecutionError(RuntimeError):
    pass


class Refused(ExecutionError):
    """A guard said no.  Not a failure: the guards are the point."""


def _iso(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# what we intend
# --------------------------------------------------------------------------- #


@dataclass
class OrderTicket:
    """One intended purchase, in the venue's own units.

    ``limit`` is a price per share on the venue's tick, ``shares`` a whole
    number of shares, and ``stake`` what those two multiply to.  The engine
    thinks in dollars; the venue thinks in shares; the conversion happens once,
    here, and rounds **down** so a rounding error can never overspend.
    """

    window_start: int
    side: str                      # "UP" or "DOWN", for the log
    token_id: str
    limit: float
    shares: int
    market_slug: str = ""
    model_probability: float | None = None
    edge: float | None = None

    @property
    def stake(self) -> float:
        return round(self.limit * self.shares, 6)

    @classmethod
    def from_decision(cls, *, window_start: int, side: str, token_id: str,
                      ask: float, stake: float, tick: float = 0.01,
                      min_shares: int = 5, market_slug: str = "",
                      model_probability: float | None = None,
                      edge: float | None = None) -> "OrderTicket":
        """Convert a dollar stake at an ask into whole shares at a limit."""
        if side not in ("UP", "DOWN"):
            raise Refused(f"side must be UP or DOWN, got {side!r}")
        if not 0.0 < ask < 1.0:
            raise Refused(f"ask {ask} is not a contract price")
        if not token_id:
            raise Refused("no token id for that side")
        # The ask already sits on the venue's tick; if it does not, the book is
        # not what we think it is, and guessing a direction to round is worse
        # than stopping.
        steps = ask / tick
        if abs(steps - round(steps)) > 1e-6:
            raise Refused(f"ask {ask} is not a multiple of the {tick} tick")
        shares = int(stake // ask)             # down, never up
        if shares < min_shares:
            raise Refused(f"{stake:.2f} at {ask} is {shares} shares; "
                          f"the venue's minimum is {min_shares}")
        return cls(window_start=window_start, side=side, token_id=token_id,
                   limit=round(ask, 4), shares=shares, market_slug=market_slug,
                   model_probability=model_probability, edge=edge)

    def describe(self) -> str:
        return (f"BUY {self.side} {self.shares} shares at {self.limit:.2f} "
                f"= ${self.stake:,.2f}   window {self.window_start}")


@dataclass
class Fill:
    """What came back.  ``shares`` is None when the venue did not say."""

    ticket: OrderTicket
    placed: bool
    order_id: str = ""
    status: str = ""
    shares: float | None = None
    price: float | None = None
    error: str = ""
    raw: Any = None

    @property
    def cost(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return round(self.shares * self.price, 6)


# --------------------------------------------------------------------------- #
# the rails
# --------------------------------------------------------------------------- #


@dataclass
class GuardState:
    """What the guard has to remember across restarts."""

    day: str = ""
    realised_loss: float = 0.0     # dollars at risk today, positive number
    staked_today: float = 0.0
    windows: list[int] = field(default_factory=list)
    orders: int = 0

    @classmethod
    def load(cls, path: Path) -> "GuardState":
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1))
        tmp.replace(path)          # atomic: a crash mid-write cannot lose the day


class Guard:
    """Every reason not to place an order, in one place.

    Defaults refuse.  ``armed`` has to be set explicitly, and each limit is a
    number the operator chose rather than one inferred from the config, because
    the config is tuned for a backtest and this is a wallet.
    """

    def __init__(self, *, armed: bool = False, max_stake: float = 10.0,
                 max_orders_per_day: int = 40, daily_loss_limit: float = 40.0,
                 balance_ceiling: float | None = None,
                 state_path: str | Path = "state/execution.json",
                 kill_file: str | Path = KILL_FILE,
                 now: Callable[[], float] = time.time):
        if max_stake <= 0 or daily_loss_limit <= 0 or max_orders_per_day <= 0:
            raise ValueError("every limit must be positive")
        self.armed = armed
        self.max_stake = max_stake
        self.max_orders_per_day = max_orders_per_day
        self.daily_loss_limit = daily_loss_limit
        self.balance_ceiling = balance_ceiling
        self.state_path = Path(state_path)
        self.kill_file = Path(kill_file)
        self.now = now
        self.state = GuardState.load(self.state_path)
        self._roll_day()

    def _roll_day(self) -> None:
        today = _iso(self.now())[:10]
        if self.state.day != today:
            self.state = GuardState(day=today)
            self.state.save(self.state_path)

    # -- the refusals ---------------------------------------------------- #

    def check(self, ticket: OrderTicket) -> None:
        """Raise ``Refused`` with the reason, or return quietly."""
        self._roll_day()
        if self.kill_file.exists():
            raise Refused(f"the kill file {self.kill_file} exists; "
                          "delete it to resume trading")
        if not self.armed:
            raise Refused("not armed: this is a dry run. Nothing was placed")
        if ticket.stake > self.max_stake + 1e-9:
            raise Refused(f"${ticket.stake:,.2f} is over the ${self.max_stake:,.2f} "
                          f"per-order cap")
        if ticket.window_start in self.state.windows:
            raise Refused(f"window {ticket.window_start} already has an order today")
        if self.state.orders >= self.max_orders_per_day:
            raise Refused(f"{self.state.orders} orders already today, "
                          f"the cap is {self.max_orders_per_day}")
        if self.state.realised_loss >= self.daily_loss_limit:
            raise Refused(f"down ${self.state.realised_loss:,.2f} today, "
                          f"at or past the ${self.daily_loss_limit:,.2f} limit")
        remaining = self.daily_loss_limit - self.state.realised_loss
        if ticket.stake > remaining + 1e-9:
            raise Refused(f"${ticket.stake:,.2f} could take today's loss past the "
                          f"${self.daily_loss_limit:,.2f} limit "
                          f"(${remaining:,.2f} of room left)")

    def check_balance(self, balance: float | None) -> None:
        """Refuse to trade an account holding more than was declared.

        A wallet funded by mistake with ten times the intended amount is the
        cheapest large loss available, and the venue will not stop it.
        """
        if self.balance_ceiling is None or balance is None:
            return
        if balance > self.balance_ceiling + 1e-9:
            raise Refused(
                f"the account holds ${balance:,.2f}, above the "
                f"${self.balance_ceiling:,.2f} ceiling you declared. Move the "
                "excess out, or raise --balance-ceiling deliberately")

    # -- bookkeeping ----------------------------------------------------- #

    def record(self, ticket: OrderTicket, fill: Fill) -> None:
        if not fill.placed:
            return
        self.state.windows.append(ticket.window_start)
        self.state.orders += 1
        self.state.staked_today = round(self.state.staked_today + ticket.stake, 6)
        # A binary bought and held can lose its whole stake, so the stake is
        # charged against the day's loss budget the moment it is placed rather
        # than when it settles.  The budget is a risk limit, not an accountant.
        self.state.realised_loss = round(self.state.realised_loss + ticket.stake, 6)
        self.state.save(self.state_path)

    def credit(self, amount: float) -> None:
        """Give back the stake of a bet that won, once it has settled."""
        self.state.realised_loss = round(max(0.0, self.state.realised_loss - amount), 6)
        self.state.save(self.state_path)

    def summary(self) -> str:
        return (f"armed {self.armed}   orders today {self.state.orders}/"
                f"{self.max_orders_per_day}   at risk today "
                f"${self.state.realised_loss:,.2f}/${self.daily_loss_limit:,.2f}   "
                f"per-order cap ${self.max_stake:,.2f}")


# --------------------------------------------------------------------------- #
# brokers
# --------------------------------------------------------------------------- #


class Broker(Protocol):
    def place(self, ticket: OrderTicket) -> Fill: ...
    def collateral(self) -> float | None: ...


class DryRunBroker:
    """Places nothing.  The default, and what a first day on a new box uses."""

    name = "dry-run"

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.tickets: list[OrderTicket] = []

    def place(self, ticket: OrderTicket) -> Fill:
        self.tickets.append(ticket)
        self.log(f"  DRY RUN, nothing placed: {ticket.describe()}")
        return Fill(ticket=ticket, placed=False, status="dry-run")

    def collateral(self) -> float | None:
        return None


class ClobBroker:
    """Polymarket's own client, wrapped thinly.

    The wrapping is deliberately thin: order signing and credential derivation
    are cryptography against a live venue, and reimplementing them from memory
    would be the most dangerous code in this repository.  This class converts a
    ticket, calls the client, and refuses to interpret an answer it does not
    recognise.
    """

    name = "polymarket-clob"

    def __init__(self, *, key: str | None = None, host: str = CLOB_HOST,
                 chain_id: int = CHAIN_POLYGON, signature_type: int | None = None,
                 funder: str | None = None, log: Callable[[str], None] = print):
        key = key or os.environ.get(KEY_ENV)
        if not key:
            raise ExecutionError(
                f"no signing key. Set {KEY_ENV} in the process environment -- "
                "from a systemd credential, never from a file in this repository "
                "and never on the command line, where it would reach the shell "
                "history and the process list.")
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise ExecutionError(
                "py-clob-client is not installed. `pip install py-clob-client`; "
                "it carries the order signing, which this package does not "
                "reimplement.") from exc
        self.log = log
        self._client = ClobClient(host, chain_id=chain_id, key=key,
                                  signature_type=signature_type, funder=funder)
        # Derive the level-2 credentials from the key rather than asking the
        # operator to create and paste three more secrets.
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        self.address = getattr(self._client, "get_address", lambda: "")()

    def place(self, ticket: OrderTicket) -> Fill:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        args = OrderArgs(token_id=ticket.token_id, price=ticket.limit,
                         size=float(ticket.shares), side=BUY)
        try:
            signed = self._client.create_order(args)
            raw = self._client.post_order(signed, ORDER_TYPE)
        except Exception as exc:                      # the client raises its own
            return Fill(ticket=ticket, placed=False,
                        error=f"{type(exc).__name__}: {exc}")
        return self._read(ticket, raw)

    @staticmethod
    def _read(ticket: OrderTicket, raw: Any) -> Fill:
        """Read a response without assuming its shape.

        An unrecognised answer is reported as placed-but-unknown, never as a
        clean fill: the journal keeps the raw body so the first live order
        settles what the fields are actually called.
        """
        if not isinstance(raw, dict):
            return Fill(ticket=ticket, placed=False,
                        error=f"unrecognised response {type(raw).__name__}", raw=raw)
        success = raw.get("success")
        error = raw.get("errorMsg") or raw.get("error") or ""
        order_id = str(raw.get("orderID") or raw.get("orderId") or raw.get("id") or "")
        status = str(raw.get("status") or "")
        made = raw.get("makingAmount")
        taken = raw.get("takingAmount")
        shares = None
        price = None
        try:
            if taken is not None and made is not None and float(taken) > 0:
                # Buying: collateral out, shares in.  Which field is which is
                # confirmed by the first live order, so both are kept raw.
                shares, price = float(taken), float(made) / float(taken)
        except (TypeError, ValueError, ZeroDivisionError):
            shares, price = None, None
        placed = bool(success) or bool(order_id)
        return Fill(ticket=ticket, placed=placed, order_id=order_id,
                    status=status or ("accepted" if placed else "rejected"),
                    shares=shares, price=price,
                    error="" if placed else (error or "no order id in the response"),
                    raw=raw)

    def collateral(self) -> float | None:
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            answer = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        except Exception as exc:
            self.log(f"  could not read the account balance ({exc}); "
                     "the balance ceiling cannot be enforced this run")
            return None
        if not isinstance(answer, dict):
            return None
        for key in ("balance", "available", "collateral"):
            if key in answer:
                try:
                    value = float(answer[key])
                except (TypeError, ValueError):
                    continue
                # The CLOB reports six-decimal units for a six-decimal token.
                return value / 1e6 if value > 1e5 else value
        return None


# --------------------------------------------------------------------------- #
# the trader
# --------------------------------------------------------------------------- #


class Trader:
    """Guard, then broker, then journal.  In that order, every time."""

    def __init__(self, broker: Broker, guard: Guard, *,
                 journal: str | Path = "orders.jsonl",
                 log: Callable[[str], None] = print):
        self.broker = broker
        self.guard = guard
        self.journal = Path(journal)
        self.log = log
        self.placed = 0
        self.refused = 0
        self._balance_checked = False

    def submit(self, ticket: OrderTicket) -> Fill:
        try:
            self.guard.check(ticket)
            self._check_balance_once()
        except Refused as exc:
            self.refused += 1
            fill = Fill(ticket=ticket, placed=False, status="refused", error=str(exc))
            self._write(fill, "refused")
            self.log(f"  not placed: {exc}")
            return fill

        self._write(Fill(ticket=ticket, placed=False, status="submitting"), "submitting")
        fill = self.broker.place(ticket)
        self.guard.record(ticket, fill)
        self._write(fill, "placed" if fill.placed else "failed")
        if fill.placed:
            self.placed += 1
            self.log(f"  PLACED {ticket.describe()}   order {fill.order_id or '?'} "
                     f"status {fill.status}")
        else:
            self.log(f"  FAILED to place: {fill.error}")
        return fill

    def _check_balance_once(self) -> None:
        if self._balance_checked:
            return
        self._balance_checked = True
        self.guard.check_balance(self.broker.collateral())

    def _write(self, fill: Fill, event: str) -> None:
        """Append to the journal before and after, so a crash leaves a trace.

        Every order that money could have gone through is on disk before the
        network call, not after it.
        """
        row = {
            "at": _iso(), "event": event, "broker": getattr(self.broker, "name", "?"),
            "window_start": fill.ticket.window_start, "side": fill.ticket.side,
            "limit": fill.ticket.limit, "shares": fill.ticket.shares,
            "stake": fill.ticket.stake, "token_id": fill.ticket.token_id,
            "slug": fill.ticket.market_slug,
            "p_model": fill.ticket.model_probability, "edge": fill.ticket.edge,
            "placed": fill.placed, "order_id": fill.order_id, "status": fill.status,
            "filled_shares": fill.shares, "filled_price": fill.price,
            "error": fill.error, "raw": fill.raw,
        }
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
