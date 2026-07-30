"""Standing paper-trading DRIVER for the Railway app service (headless).

This is the process that actually trades: it wraps the day-aware loop
(:func:`pipeline.sim.daily.run_daily`) with the intraday session that was never
checked in before, wires the guardrailed broker + Alpaca clock/quotes, and makes
the whole thing survive a mid-market container restart.

Order placement lives ONLY here, in an internal clock loop — never behind an HTTP
route. The web service imports none of this in a request path; the driver runs as
its own process (see scripts/run_trader.py + scripts/railway_start.sh), gated by
``TRADER_DRIVER_ENABLED`` (default off → zero behavior change).

Restart safety (the cloud-specific crux). A Railway redeploy can kill this
process mid-session. On the next boot nothing double-enters or over-trades,
because the durable state lives in the volume DB + Alpaca, not in memory:

  * Double-entry: evaluate_entries dedupes against OPEN sim_trades and a 24h
    re-entry cooldown (both read from the DB), so resuming the session re-enters
    nothing it already holds — it only catches up on genuinely new clusters.
  * Loss caps: the per-config and portfolio caps are recomputed each sweep from
    today's CLOSED sim_trades (ledger-derived), so a restart respects the caps
    already consumed today without any separate counter to persist or corrupt.
  * Stranded positions: on boot we reconcile Alpaca positions/orders against the
    DB and log mismatches; the EOD flatten runs the engine's force-exit AND
    broker.flatten_all() as a backstop, so no position outlives the session even
    if its DB row went missing.

The only in-memory thing that resets on restart is the broker's per-RUN order
cap (a runaway-loop circuit breaker, not a daily limit); real exposure stays
bounded by the DB dedup, the loss caps, and Alpaca's max-open cap.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pipeline.common.models import SimTrade, TraderHeartbeat
from pipeline.common.timeutil import utcnow
from pipeline.sim.daily import run_daily, session_bounds_utc, write_daily_summary
from pipeline.sim.engine import run_sim_cycle

log = logging.getLogger("pipeline.sim.driver")

DEFAULT_SWEEP_INTERVAL_S = 60.0


def driver_enabled() -> bool:
    """Master gate for the standing driver. Default OFF — unset ==> the app
    service behaves exactly as before (API + pipeline, no trading)."""
    return (os.environ.get("TRADER_DRIVER_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def sweep_interval_s() -> float:
    raw = os.environ.get("TRADER_DRIVER_SWEEP_S")
    if raw and raw.strip():
        try:
            return max(5.0, float(raw))
        except ValueError:
            log.warning("invalid TRADER_DRIVER_SWEEP_S=%r — using %.0fs", raw, DEFAULT_SWEEP_INTERVAL_S)
    return DEFAULT_SWEEP_INTERVAL_S


def ensure_heartbeat_table(engine: Engine) -> None:
    """Create trader_heartbeat if missing (idempotent) — the long-lived prod DB
    predates it and create_all only runs at init. Mirrors ensure_watchlist_table."""
    try:
        TraderHeartbeat.__table__.create(bind=engine, checkfirst=True)
    except Exception:  # noqa: BLE001 — never block startup on this
        pass


def make_driver_id(host: str, pid: int, started_at: datetime) -> str:
    return f"{host}:{pid}:{int(started_at.timestamp())}"


# --------------------------------------------------------------------------- #
# heartbeat (liveness + cheap double-driver signal)
# --------------------------------------------------------------------------- #
def write_heartbeat(
    session: Session,
    *,
    driver_id: str,
    host: str,
    pid: int,
    started_at: datetime,
    now: datetime,
    sweeps: int,
    note: str,
    session_date: str | None,
    stale_after_s: float,
) -> bool:
    """Upsert the single heartbeat row. Returns True (and logs loudly) when a
    DIFFERENT driver_id beat this same DB within ``stale_after_s`` — i.e. two
    drivers are trading the same account. (Only catches drivers on THIS DB; a
    local driver on a separate .db is invisible here — hence the docs rule.)"""
    row = session.get(TraderHeartbeat, "driver")
    conflict = False
    if row is not None and row.driver_id != driver_id:
        age = (now - row.last_beat).total_seconds()
        if age < stale_after_s:
            conflict = True
            log.warning(
                "DOUBLE-DRIVER DETECTED: another driver %r beat %.0fs ago on this database. "
                "Only ONE driver may trade a paper account — stop the other now.",
                row.driver_id, age,
            )
    if row is None:
        row = TraderHeartbeat(id="driver")
        session.add(row)
    row.driver_id = driver_id
    row.host = host
    row.pid = pid
    row.started_at = started_at
    row.last_beat = now
    row.sweeps = sweeps
    row.note = note
    row.session_date = session_date
    row.conflict = conflict
    session.commit()
    return conflict


def read_heartbeat(session: Session, *, now: datetime, stale_after_s: float) -> dict[str, Any]:
    """Read-only driver liveness for the web layer. ``alive`` = a beat within
    ``stale_after_s``. Never raises (table may not exist pre-first-run)."""
    try:
        row = session.get(TraderHeartbeat, "driver")
    except Exception:  # noqa: BLE001
        return {"present": False, "alive": False}
    if row is None:
        return {"present": False, "alive": False}
    age = (now - row.last_beat).total_seconds()
    return {
        "present": True,
        "alive": age <= stale_after_s,
        "age_s": round(age, 1),
        "driver_id": row.driver_id,
        "host": row.host,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "last_beat": row.last_beat.isoformat() if row.last_beat else None,
        "sweeps": row.sweeps,
        "session_date": row.session_date,
        "note": row.note,
        "conflict": bool(row.conflict),
    }


# --------------------------------------------------------------------------- #
# boot reconciliation
# --------------------------------------------------------------------------- #
def reconcile_on_boot(session: Session, reader: Any, *, now: datetime) -> dict[str, Any]:
    """Reconcile the live Alpaca book against the DB on driver boot. Read-only:
    it LOGS mismatches (orphan Alpaca positions with no open sim_trade; open
    sim_trades with no Alpaca position) rather than mutating the immutable ledger.
    Orphans are swept up by the EOD flatten_all backstop; missing positions make
    the next exit a no-op. The point is a loud, honest picture after a restart."""
    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    try:
        positions = reader.positions()
    except Exception:  # noqa: BLE001
        log.warning("reconcile: could not read Alpaca positions", exc_info=True)
    try:
        open_orders = reader.orders(status="open")
    except Exception:  # noqa: BLE001
        log.warning("reconcile: could not read Alpaca open orders", exc_info=True)

    alp = {str(p.get("symbol", "")).upper() for p in positions if p.get("symbol")}
    db_open = session.execute(select(SimTrade).where(SimTrade.status == "open")).scalars().all()
    dbt = {t.ticker.upper() for t in db_open}
    orphans = sorted(alp - dbt)
    missing = sorted(dbt - alp)
    summary = {
        "alpaca_positions": len(positions),
        "alpaca_open_orders": len(open_orders),
        "db_open_trades": len(db_open),
        "orphans": orphans,
        "missing": missing,
    }
    log.info("driver boot reconcile: %s", summary)
    if orphans:
        log.warning(
            "reconcile: %d Alpaca position(s) with no open sim_trade %s — the EOD "
            "flatten_all backstop will close them.", len(orphans), orphans,
        )
    if missing:
        log.warning(
            "reconcile: %d open sim_trade(s) with no Alpaca position %s — closed "
            "out-of-band; their exits will no-op.", len(missing), missing,
        )
    return summary


# --------------------------------------------------------------------------- #
# the intraday session (this is the session_fn run_daily was missing)
# --------------------------------------------------------------------------- #
def run_session(
    flatten_at: datetime | None,
    clock: dict[str, Any],
    *,
    session_factory: Callable[[], Session],
    reader: Any,
    broker: Any,
    quote: Callable[[str], float | None],
    driver_id: str,
    host: str,
    pid: int,
    started_at: datetime,
    sleep: Callable[[float], Any] = time.sleep,
    now_fn: Callable[[], datetime] = utcnow,
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    stale_after_s: float = 180.0,
    max_sweeps: int | None = None,
) -> int:
    """Run ONE trading day: reconcile on boot, sweep entries+exits until the
    flatten cutoff, then force-flatten (engine exit + broker.flatten_all backstop).
    Blocks until flatten. Returns the number of sweeps run.

    Refuses to trade without a known flatten cutoff: a driver that can't guarantee
    it will flatten before the close must not open positions."""
    if flatten_at is None:
        log.warning("no flatten cutoff for this session (clock lacked next_close) — skipping to be safe")
        return 0

    with session_factory() as s:
        reconcile_on_boot(s, reader, now=now_fn())

    ts = clock.get("timestamp")
    session_date = ts.date().isoformat() if ts else None
    sweeps = 0
    while True:
        now = now_fn()
        force = now >= flatten_at
        with session_factory() as s:
            run_sim_cycle(s, quote, now=now, broker=broker, force_exit=force)
            write_heartbeat(
                s, driver_id=driver_id, host=host, pid=pid, started_at=started_at,
                now=now, sweeps=sweeps, note=("flatten" if force else "sweep"),
                session_date=session_date, stale_after_s=stale_after_s,
            )
        sweeps += 1
        if force:
            # Backstop: close ANY Alpaca position still standing, even one whose DB
            # row went missing across a restart. Independent of the ledger.
            try:
                closed = broker.flatten_all()
                if closed:
                    log.info("flatten backstop closed %d residual Alpaca position(s)", closed)
            except Exception:  # noqa: BLE001
                log.warning("flatten_all backstop failed", exc_info=True)
            break
        if max_sweeps is not None and sweeps >= max_sweeps:
            break
        sleep(sweep_interval_s)
    return sweeps


# --------------------------------------------------------------------------- #
# top-level driver: wire real clients into run_daily
# --------------------------------------------------------------------------- #
def run_trader_driver(
    *,
    engine: Engine,
    sleep: Callable[[float], Any] = time.sleep,
    now_fn: Callable[[], datetime] = utcnow,
    max_iters: int | None = None,
) -> Any:
    """Construct the paper broker + read clients (all paper-endpoint-asserted) and
    run the standing daily loop. Refuses to start unless Alpaca keys are present
    and the account passes assert_paper_ready (ACTIVE, unblocked, buying power)."""
    from pipeline.marketdata.alpaca import AlpacaData, alpaca_configured
    from pipeline.marketdata.paper_account import PaperAccountReader
    from pipeline.sim.broker import AlpacaPaperBroker

    if not alpaca_configured():
        log.error("TRADER driver: Alpaca keys absent — refusing to start (set ALPACA_API_KEY/SECRET)")
        return None

    ensure_heartbeat_table(engine)
    data = AlpacaData()
    reader = PaperAccountReader()
    broker = AlpacaPaperBroker()
    acct = broker.assert_paper_ready()  # raises unless ACTIVE + unblocked + buying power
    log.info("TRADER driver: paper account ready (%s) — endpoint %s", acct.get("account_number"), acct.get("endpoint"))

    host = socket.gethostname()
    pid = os.getpid()
    started_at = now_fn()
    driver_id = make_driver_id(host, pid, started_at)
    interval = sweep_interval_s()
    stale_after = max(120.0, interval * 3)
    log.info("TRADER driver starting: id=%s sweep=%.0fs", driver_id, interval)

    def quote(t: str) -> float | None:
        tr = data.latest_trade(t)
        return tr.get("price") if tr else None

    def clock_fn() -> dict[str, Any] | None:
        return data.clock()  # parsed aware datetimes (ET-offset ISO) or None

    def session_fn(flatten_at: datetime | None, clock: dict[str, Any]) -> Any:
        return run_session(
            flatten_at, clock,
            session_factory=lambda: Session(engine, expire_on_commit=False),
            reader=reader, broker=broker, quote=quote, driver_id=driver_id,
            host=host, pid=pid, started_at=started_at, sleep=sleep, now_fn=now_fn,
            sweep_interval_s=interval, stale_after_s=stale_after,
        )

    def summary_fn(session_date: Any, clock: dict[str, Any]) -> Any:
        ts = clock.get("timestamp")
        tzinfo = ts.tzinfo if ts is not None else UTC
        day_start, day_end = session_bounds_utc(session_date, tzinfo)
        with Session(engine, expire_on_commit=False) as s:
            return write_daily_summary(s, session_date, day_start, day_end, now=now_fn())

    return run_daily(
        clock_fn=clock_fn,
        session_fn=session_fn,
        summary_fn=summary_fn,
        sleep=sleep,
        begin_run=broker.begin_run,
        max_iters=max_iters,
    )
