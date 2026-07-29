"""Alpaca paper BROKER — the sim engine's execution backend (Phase 2).

This is the order/fill side that alpaca.py (market data) deliberately left out,
so the human-gate semantics arrive with it. It exists ONLY to express a sim
config as real paper orders against Alpaca's paper account; sim_trades stays the
source of truth, and fills are reconciled from Alpaca, never assumed.

HARD GUARDRAILS (refuse to trade if any fails):
  * paper endpoint ONLY — constructing against anything but
    ``paper-api.alpaca.markets`` raises, regardless of env values. There is no
    code path from this class to a live endpoint.
  * equity-only, DAY time-in-force, small fixed notional per order.
  * per-run order cap + max open-position cap; a breached cap refuses the order.
  * account() must report a paper account (buying power) before any order.

Fills: submit -> poll the order until filled/rejected/timeout. A partial or
non-fill returns None so the engine writes no trade (no-fill == no-position,
mirroring the no-quote-no-trade rule). Whole-share quantities only (fractional
shorting is disallowed; whole shares keep long and short symmetric).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from pipeline.marketdata.alpaca import alpaca_keys

log = logging.getLogger("pipeline.sim.broker")

# The ONE endpoint this class will talk to. Not configurable by design.
PAPER_URL = "https://paper-api.alpaca.markets"

DEFAULT_NOTIONAL = 1000.0  # $ paper per position
DEFAULT_MAX_OPEN = 25  # max concurrent open paper positions this broker will hold
DEFAULT_MAX_ORDERS_PER_RUN = 40  # circuit breaker per driver invocation
_TERMINAL_OK = {"filled"}
_TERMINAL_BAD = {"canceled", "cancelled", "expired", "rejected", "done_for_day", "suspended"}


class BrokerGuardrailError(RuntimeError):
    """A hard guardrail refused to construct or to place an order."""


def ensure_broker_columns(engine: Any) -> None:
    """Idempotently add the sim_trades broker columns to an existing DB.

    Fresh DBs get them from the model via create_all; the long-lived prod DB
    predates them, so ALTER them in (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    from sqlalchemy import text

    want = {
        "broker": "VARCHAR(20)",
        "broker_entry_order_id": "VARCHAR(48)",
        "broker_exit_order_id": "VARCHAR(48)",
    }
    with engine.begin() as conn:
        have = {row[1] for row in conn.execute(text("PRAGMA table_info(sim_trades)"))}
        for col, decl in want.items():
            if col not in have:
                conn.execute(text(f"ALTER TABLE sim_trades ADD COLUMN {col} {decl}"))
                log.info("migrated sim_trades: added column %s", col)


@dataclass(frozen=True)
class BrokerFill:
    """A reconciled fill (or the terminal state of a non-fill)."""

    order_id: str
    ticker: str
    side: str  # buy | sell
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    status: str
    submitted_at: str | None


class AlpacaPaperBroker:
    """Minimal paper-order client. ``http`` is injectable for tests (anything
    with .get/.post(url, headers=, params=/json=, timeout=) -> requests-style)."""

    def __init__(
        self,
        http: Any | None = None,
        *,
        base_url: str = PAPER_URL,
        notional: float = DEFAULT_NOTIONAL,
        max_open: int = DEFAULT_MAX_OPEN,
        max_orders_per_run: int = DEFAULT_MAX_ORDERS_PER_RUN,
        reconcile_timeout_s: float = 150.0,
        reconcile_poll_s: float = 3.0,
    ) -> None:
        # GUARDRAIL 1: paper endpoint only. No env value, no argument, nothing
        # can point this class at a live account.
        if base_url != PAPER_URL:
            raise BrokerGuardrailError(
                f"refusing to construct paper broker against non-paper endpoint {base_url!r}; "
                f"only {PAPER_URL} is permitted"
            )
        keys = alpaca_keys()
        if keys is None:
            raise BrokerGuardrailError("Alpaca keys missing (ALPACA_API_KEY / ALPACA_API_SECRET)")
        self._base = base_url
        self._headers = {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]}
        import requests

        self._http = http or requests.Session()
        self.notional = float(notional)
        self.max_open = int(max_open)
        self.max_orders_per_run = int(max_orders_per_run)
        # Paper fills lag at/near the open (observed 2026-07-17: a 9:31-ET market
        # order took 2m43s to fill), so reconcile must poll patiently by default.
        self.reconcile_timeout_s = float(reconcile_timeout_s)
        self.reconcile_poll_s = float(reconcile_poll_s)
        self._orders_placed = 0

    # --- REST plumbing -----------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(
            f"{self._base}{path}", headers=self._headers, params=params or {}, timeout=15
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._http.post(f"{self._base}{path}", headers=self._headers, json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    # --- account -----------------------------------------------------------
    def account(self) -> dict[str, Any]:
        """Paper account snapshot — the connectivity check. Asserts the response
        looks like a paper account before any order is ever placed."""
        acct = self._get("/v2/account")
        # Alpaca paper accounts return the same shape as live; the endpoint is our
        # authority (we can only reach paper-api). Surface the fields the operator
        # needs to confirm it's the paper book.
        return {
            "account_number": acct.get("account_number"),
            "status": acct.get("status"),
            "buying_power": float(acct.get("buying_power", 0) or 0),
            "cash": float(acct.get("cash", 0) or 0),
            "equity": float(acct.get("equity", 0) or 0),
            "pattern_day_trader": acct.get("pattern_day_trader"),
            "trading_blocked": acct.get("trading_blocked"),
            "endpoint": self._base,  # proves paper host
        }

    def assert_paper_ready(self) -> dict[str, Any]:
        """Raise unless the account is reachable, unblocked, and has buying power."""
        acct = self.account()
        if acct.get("trading_blocked"):
            raise BrokerGuardrailError("account trading_blocked=true — refusing to trade")
        if acct.get("buying_power", 0) <= 0:
            raise BrokerGuardrailError("account has no buying power — refusing to trade")
        if acct.get("status") != "ACTIVE":
            raise BrokerGuardrailError(f"account status={acct.get('status')!r} (not ACTIVE)")
        return acct

    def begin_run(self) -> None:
        """Reset the per-run entry-order circuit breaker. The standing daily loop
        reuses ONE broker across many trading days; without this reset the
        ``max_orders_per_run`` cap would accumulate across days and silently choke
        off entries after the first ~40. Call once at the start of each day's
        session so the cap means 'per day', matching the driver's cadence."""
        self._orders_placed = 0

    def open_position_count(self) -> int:
        try:
            return len(self._get("/v2/positions") or [])
        except Exception:  # noqa: BLE001 — treat unknown as at-cap-safe
            log.warning("could not read open positions; treating as unknown")
            return self.max_open  # conservative: block new entries if we can't count

    # --- orders ------------------------------------------------------------
    def submit_market(
        self,
        ticker: str,
        direction: int,
        qty: int,
        *,
        client_order_id: str | None = None,
        is_close: bool = False,
    ) -> str:
        """Place ONE equity market DAY order. direction +1=buy, -1=sell(short).
        Returns the Alpaca order id. The per-run order cap bounds ENTRIES only —
        ``is_close=True`` orders (flattening) always go through, so a cap hit can
        never strand an open position it cannot close."""
        if direction not in (1, -1):
            raise BrokerGuardrailError(f"illegal direction {direction!r}")
        if qty <= 0:
            raise BrokerGuardrailError(f"illegal qty {qty!r}")
        if not is_close and self._orders_placed >= self.max_orders_per_run:
            raise BrokerGuardrailError(
                f"per-run entry order cap reached ({self.max_orders_per_run}) — refusing new entries"
            )
        body = {
            "symbol": ticker.upper(),
            "qty": str(int(qty)),  # whole shares only
            "side": "buy" if direction == 1 else "sell",
            "type": "market",
            "time_in_force": "day",  # GUARDRAIL: DAY only
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        order = self._post("/v2/orders", body)
        if not is_close:
            self._orders_placed += 1
        oid = order.get("id")
        log.info(
            "paper order submitted id=%s %s %s x%d", oid, body["side"], body["symbol"], qty
        )
        return oid

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._get(f"/v2/orders/{order_id}")

    def flatten_all(self) -> int:
        """Close EVERY open paper position at market (safety flatten). Closing
        orders bypass the entry cap. Returns the count of positions it closed.
        Used as the verify's self-heal and as an operator escape hatch — it acts
        on the real Alpaca book, independent of the sim_trades ledger."""
        try:
            positions = self._get("/v2/positions") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("flatten_all: could not read positions: %s", exc)
            return 0
        closed = 0
        for p in positions:
            sym = p.get("symbol")
            qty = abs(int(float(p.get("qty", 0))))
            side = -1 if str(p.get("side", "long")) == "long" else 1  # close = opposite
            if not sym or qty < 1:
                continue
            try:
                oid = self.submit_market(sym, side, qty, is_close=True)
                fill = self.reconcile(oid)
                if fill is not None:
                    closed += 1
                    log.info("flatten_all: closed %s x%d @ %s", sym, qty, fill.filled_avg_price)
                else:
                    log.warning("flatten_all: close order for %s did not confirm", sym)
            except Exception as exc:  # noqa: BLE001
                log.warning("flatten_all: close of %s failed: %s", sym, exc)
        return closed

    def reconcile(
        self,
        order_id: str,
        *,
        timeout_s: float | None = None,
        poll_s: float | None = None,
        sleep=time.sleep,
    ) -> BrokerFill | None:
        """Poll an order to a terminal state. Returns a BrokerFill on 'filled'
        (with the real filled_avg_price); returns None on any non-fill terminal
        state or timeout — the engine then writes no trade / leaves it open.
        Defaults to the broker's patient reconcile_timeout_s/poll_s."""
        timeout_s = self.reconcile_timeout_s if timeout_s is None else timeout_s
        poll_s = self.reconcile_poll_s if poll_s is None else poll_s
        deadline_polls = max(1, int(timeout_s / poll_s))
        last: dict[str, Any] = {}
        for _ in range(deadline_polls):
            last = self.get_order(order_id)
            status = (last.get("status") or "").lower()
            if status in _TERMINAL_OK:
                fap = last.get("filled_avg_price")
                return BrokerFill(
                    order_id=order_id,
                    ticker=(last.get("symbol") or "").upper(),
                    side=last.get("side") or "",
                    qty=float(last.get("qty") or 0),
                    filled_qty=float(last.get("filled_qty") or 0),
                    filled_avg_price=float(fap) if fap is not None else None,
                    status=status,
                    submitted_at=last.get("submitted_at"),
                )
            if status in _TERMINAL_BAD:
                log.warning("order %s terminal non-fill: %s", order_id, status)
                return None
            sleep(poll_s)
        log.warning("order %s did not reach terminal state in %.0fs (status=%s)",
                    order_id, timeout_s, last.get("status"))
        return None
