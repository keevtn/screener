"""Standing daily paper-trading loop + EOD report-card rollup.

This is the day-aware wrapper that turns the one-shot mockup session into a
STANDING component: it consults the Alpaca trading clock (the calendar authority —
Alpaca's ET-offset timestamps natively encode weekends, holidays, half-days, and
DST) to arm exactly one session per trading day, run it, roll up the day's P&L,
then sleep to the next open. No weekend/holiday churn, no re-verify spam while
closed.

The trading itself is unchanged — the session still runs through the SAME
guardrailed path (pipeline/sim/broker.py: paper endpoint hard-asserted, DAY
orders, position/order caps, no-quote-no-trade; pipeline/sim/engine.py: immutable
ledger with reconciled fills). This module only decides WHEN to run and records
WHAT happened; it adds no new trade path.

The decision logic (:func:`plan_day`) and the rollup (:func:`write_daily_summary`)
are pure/DB-only so they unit-test against a mock clock with zero real orders.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import SimConfig, SimDailySummary, SimTrade
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.sim.daily")

DEFAULT_FLATTEN_LEAD_MIN = 10  # flatten this many minutes before the real close
DEFAULT_IDLE_CAP_S = 3600.0  # longest single sleep while closed (re-check hourly)
DEFAULT_RETRY_S = 60.0  # sleep when the clock is unavailable (don't trade blind)
DEFAULT_NOTIONAL = 1000.0  # $ fallback if a trade row lacks a snapshotted notional


def flatten_time(next_close: datetime, lead_min: int = DEFAULT_FLATTEN_LEAD_MIN) -> datetime:
    """The intraday flatten cutoff = the day's real close minus a lead. Derived
    from Alpaca's next_close so a half-day (13:00 ET) flattens at 12:50 ET and a
    normal day (16:00 ET) at 15:50 ET — market orders still fill inside RTH."""
    return next_close - timedelta(minutes=lead_min)


def session_bounds_utc(session_date: date_, tzinfo: Any) -> tuple[datetime, datetime]:
    """UTC [start, end) covering the ET trading date, using the day's actual ET
    offset (from the clock) so the window is DST-correct. Used to scope the EOD
    rollup to trades that closed during this session."""
    start_et = datetime(session_date.year, session_date.month, session_date.day, tzinfo=tzinfo)
    start_utc = start_et.astimezone(UTC)
    return start_utc, start_utc + timedelta(days=1)


@dataclass(frozen=True)
class DayAction:
    """What the loop should do this tick."""

    kind: str  # "session" | "idle" | "retry"
    wait_s: float  # how long to sleep (idle/retry); 0 for session
    flatten_at: datetime | None  # session: the intraday flatten cutoff
    session_date: date_ | None  # the ET trading date this tick refers to


def plan_day(
    clock: dict[str, Any] | None,
    last_date: date_ | None,
    *,
    flatten_lead_min: int = DEFAULT_FLATTEN_LEAD_MIN,
    idle_cap_s: float = DEFAULT_IDLE_CAP_S,
    retry_s: float = DEFAULT_RETRY_S,
) -> DayAction:
    """Pure decision from a clock snapshot: run today's session, idle to the next
    open, or retry (clock unavailable). Arms at most ONE session per trading date
    via ``last_date`` — an already-run day idles even while the market is still
    open, so a mid-day restart never double-trades the same session."""
    if not clock or clock.get("timestamp") is None:
        return DayAction("retry", float(retry_s), None, None)
    ts: datetime = clock["timestamp"]
    sd = ts.date()  # ET date (Alpaca timestamp is ET-offset aware)
    if clock.get("is_open") and sd != last_date:
        nc = clock.get("next_close")
        return DayAction("session", 0.0, flatten_time(nc, flatten_lead_min) if nc else None, sd)
    nxt = clock.get("next_open")
    if nxt is None:
        return DayAction("idle", float(idle_cap_s), None, sd)
    # Floor at retry_s so we never busy-loop; cap at idle_cap_s so a long overnight
    # wait still re-checks periodically (and logs sparingly) rather than one huge sleep.
    wait = max(float(retry_s), min((nxt - ts).total_seconds(), float(idle_cap_s)))
    return DayAction("idle", wait, None, sd)


def write_daily_summary(
    session: Session,
    session_date: date_,
    day_start_utc: datetime,
    day_end_utc: datetime,
    *,
    gate_ref: str | None = None,
    spy_ref: float | None = None,
    default_notional: float = DEFAULT_NOTIONAL,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Roll up the day's realized paper trades into one sim_daily_summary row per
    config that closed a trade in the session window, and upsert them (idempotent —
    a re-run at a late flatten just refreshes). Returns the rows as dicts for logging.

    P&L is honest-costs net (COST_RT is already in net_return); pnl_dollars weights
    each trade by its snapshotted notional. Configs with no realized trade that day
    get no row (a row means 'this config traded today')."""
    from pipeline.sim.engine import config_loss_cap_usd

    now = now or utcnow()
    cfg_cap = config_loss_cap_usd()
    configs = {c.config_id: c for c in session.execute(select(SimConfig)).scalars()}
    closed = session.execute(
        select(SimTrade).where(
            SimTrade.status == "closed",
            SimTrade.exited_at >= day_start_utc,
            SimTrade.exited_at < day_end_utc,
        )
    ).scalars().all()
    open_now = session.execute(
        select(SimTrade).where(SimTrade.status == "open")
    ).scalars().all()

    by_closed: dict[str, list[SimTrade]] = {}
    for t in closed:
        by_closed.setdefault(t.config_id, []).append(t)
    open_by_cfg: dict[str, int] = {}
    for t in open_now:
        open_by_cfg[t.config_id] = open_by_cfg.get(t.config_id, 0) + 1

    rows: list[dict[str, Any]] = []
    for cid, trades in by_closed.items():
        cfg = configs.get(cid)
        name = cfg.name if cfg else cid
        nets = [t.net_return for t in trades if t.net_return is not None]
        wins = sum(1 for n in nets if n > 0)
        losses = sum(1 for n in nets if n <= 0)
        pnl = sum(
            (t.net_return or 0.0)
            * float((t.features_json or {}).get("notional") or default_notional)
            for t in trades
        )
        vals = dict(
            config_name=name,
            trades=len(trades),
            open_eod=open_by_cfg.get(cid, 0),
            wins=wins,
            losses=losses,
            hit_rate=round(wins / len(nets), 4) if nets else None,
            mean_net=round(sum(nets) / len(nets), 6) if nets else None,
            sum_net=round(sum(nets), 6) if nets else None,
            pnl_dollars=round(pnl, 2) if nets else None,
            spy_ref=spy_ref,
            gate_ref=gate_ref,
            updated_at=now,
        )
        session.execute(
            sqlite_insert(SimDailySummary)
            .values(session_date=session_date, config_id=cid, **vals)
            .on_conflict_do_update(
                index_elements=[SimDailySummary.session_date, SimDailySummary.config_id],
                set_=vals,
            )
        )
        # Surface the entry-guard verdict for the day (derived, not persisted): a
        # config whose realized $ loss reached the cap had its entries halted.
        entry_capped = bool(cfg_cap > 0 and (vals["pnl_dollars"] or 0.0) <= -cfg_cap)
        rows.append(
            {
                "config": name,
                "session_date": session_date.isoformat(),
                "entry_capped": entry_capped,
                **vals,
            }
        )
    session.commit()
    return rows


def run_daily(
    *,
    clock_fn: Callable[[], dict[str, Any] | None],
    session_fn: Callable[[datetime | None, dict[str, Any]], Any],
    summary_fn: Callable[[date_, dict[str, Any]], Any],
    sleep: Callable[[float], Any] = time.sleep,
    begin_run: Callable[[], Any] | None = None,
    flatten_lead_min: int = DEFAULT_FLATTEN_LEAD_MIN,
    idle_cap_s: float = DEFAULT_IDLE_CAP_S,
    retry_s: float = DEFAULT_RETRY_S,
    max_iters: int | None = None,
) -> date_ | None:
    """The standing loop. Each tick: read the clock, plan, then either run today's
    session (once per trading date) or sleep to the next open. Everything mutable
    is injected so the whole loop unit-tests against a mock clock with no real
    broker. Returns the last session date run (useful for tests)."""
    last_date: date_ | None = None
    it = 0
    while max_iters is None or it < max_iters:
        it += 1
        clock = clock_fn()
        action = plan_day(
            clock, last_date,
            flatten_lead_min=flatten_lead_min, idle_cap_s=idle_cap_s, retry_s=retry_s,
        )
        if action.kind == "session":
            log.info("arming paper session for %s (flatten at %s)",
                     action.session_date, action.flatten_at)
            if begin_run is not None:
                begin_run()  # reset the broker's per-day order cap
            session_fn(action.flatten_at, clock)  # runs verify + sweeps + flatten (blocks the day)
            summary_fn(action.session_date, clock)  # EOD report-card rollup
            last_date = action.session_date
            log.info("session %s complete — summary written; sleeping to next open", last_date)
            continue
        if action.kind == "retry":
            log.warning("trading clock unavailable — retrying in %.0fs (not trading blind)",
                        action.wait_s)
        else:
            log.info("market closed / session done — idling %.0fs", action.wait_s)
        sleep(action.wait_s)
    return last_date
