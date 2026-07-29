"""Standing daily paper-sim: clock-driven day planner, EOD rollup, loop (mock clock).

Pure/DB-only — no real broker, no orders. Proves the scheduler arms exactly one
session per trading day, idles the rest, derives the flatten cutoff from the real
close (half-day/DST safe), and rolls the day's trades into sim_daily_summary."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import SimConfig, SimDailySummary, SimTrade
from pipeline.sim.daily import (
    flatten_time,
    plan_day,
    run_daily,
    session_bounds_utc,
    write_daily_summary,
)

EDT = timezone(timedelta(hours=-4))  # the ET offset Alpaca returns in July


def _clock(is_open, ts, next_open, next_close):
    return {"is_open": is_open, "timestamp": ts, "next_open": next_open, "next_close": next_close}


# --- flatten cutoff (calendar-derived) ---------------------------------------


def test_flatten_time_normal_and_halfday():
    close = datetime(2026, 7, 20, 16, 0, tzinfo=EDT)
    assert flatten_time(close) == datetime(2026, 7, 20, 15, 50, tzinfo=EDT)  # normal 10-min lead
    half = datetime(2026, 11, 27, 13, 0, tzinfo=EDT)  # early close (day after Thanksgiving)
    assert flatten_time(half) == datetime(2026, 11, 27, 12, 50, tzinfo=EDT)  # still inside RTH


# --- pure day planner --------------------------------------------------------


def test_plan_open_new_day_arms_session():
    ts = datetime(2026, 7, 20, 10, 0, tzinfo=EDT)
    close = datetime(2026, 7, 20, 16, 0, tzinfo=EDT)
    nopen = datetime(2026, 7, 21, 9, 30, tzinfo=EDT)
    a = plan_day(_clock(True, ts, nopen, close), last_date=None)
    assert a.kind == "session" and a.session_date == date(2026, 7, 20)
    assert a.flatten_at == datetime(2026, 7, 20, 15, 50, tzinfo=EDT)


def test_plan_already_ran_today_idles_even_if_open():
    # Market still open, but today's session already ran -> never double-trade it.
    ts = datetime(2026, 7, 20, 15, 55, tzinfo=EDT)
    nopen = datetime(2026, 7, 21, 9, 30, tzinfo=EDT)
    close = datetime(2026, 7, 20, 16, 0, tzinfo=EDT)
    a = plan_day(_clock(True, ts, nopen, close), last_date=date(2026, 7, 20))
    assert a.kind == "idle"


def test_plan_closed_idles_until_next_open_capped():
    ts = datetime(2026, 7, 19, 12, 0, tzinfo=EDT)  # weekend, closed
    nopen = datetime(2026, 7, 20, 9, 30, tzinfo=EDT)  # ~21.5h away
    a = plan_day(_clock(False, ts, nopen, None), last_date=None, idle_cap_s=3600)
    assert a.kind == "idle" and a.wait_s == 3600  # capped — no single giant sleep, no spam


def test_plan_none_clock_retries():
    a = plan_day(None, last_date=None, retry_s=45)
    assert a.kind == "retry" and a.wait_s == 45  # never trades on an unknown clock


def test_session_bounds_utc_dst_correct():
    start, end = session_bounds_utc(date(2026, 7, 20), EDT)
    assert start == datetime(2026, 7, 20, 4, 0, tzinfo=UTC)  # 00:00 EDT == 04:00 UTC
    assert end == datetime(2026, 7, 21, 4, 0, tzinfo=UTC)


# --- EOD rollup --------------------------------------------------------------


def _seed_cfg(s, name):
    c = SimConfig(name=name, created_at=datetime(2026, 7, 1, tzinfo=UTC),
                  params_json={}, enabled=True, gate_ref="exp")
    s.add(c)
    s.flush()
    return c.config_id


def _trade(s, cid, ticker, entered, net, *, notional=1000.0, exited=None):
    s.add(SimTrade(
        config_id=cid, ticker=ticker, direction=1, entered_at=entered,
        entry_price=100.0, entry_source="alpaca-paper", horizon_trading_days=0,
        features_json={"notional": notional}, status="closed",
        exited_at=exited, exit_price=100.0 * (1 + net), exit_reason="close",
        gross_return=net, net_return=net, created_at=entered, broker="alpaca-paper",
    ))


def test_write_daily_summary_rollup_and_idempotent(engine):
    d = date(2026, 7, 20)
    start, end = session_bounds_utc(d, EDT)
    mid = datetime(2026, 7, 20, 19, 45, tzinfo=UTC)  # ~15:45 ET, inside the session window
    with Session(engine) as s:
        cid = _seed_cfg(s, "exp-a")
        _trade(s, cid, "AAA", start + timedelta(hours=10), 0.02, exited=mid)
        _trade(s, cid, "BBB", start + timedelta(hours=10), -0.01, exited=mid)
        _trade(s, cid, "CCC", start + timedelta(hours=10), 0.03, exited=mid)
        # a trade closed on a DIFFERENT day must not leak into this day's card
        _trade(s, cid, "OLD", start - timedelta(days=1), 0.99,
               exited=start - timedelta(days=1, hours=-1))
        s.commit()

        rows = write_daily_summary(s, d, start, end, gate_ref="exp", spy_ref=500.0)
        assert len(rows) == 1
        r = rows[0]
        assert r["trades"] == 3 and r["wins"] == 2 and r["losses"] == 1
        assert r["hit_rate"] == round(2 / 3, 4)
        assert abs(r["sum_net"] - (0.02 - 0.01 + 0.03)) < 1e-9
        assert abs(r["pnl_dollars"] - 40.0) < 1e-6  # (0.02-0.01+0.03)*$1000

        row = s.get(SimDailySummary, (d, cid))
        assert row is not None and row.trades == 3 and row.spy_ref == 500.0

    # re-run upserts (no duplicate, refreshes the tape ref)
    with Session(engine) as s:
        write_daily_summary(s, d, start, end, gate_ref="exp", spy_ref=501.0)
        all_rows = s.execute(select(SimDailySummary)).scalars().all()
        assert len(all_rows) == 1 and all_rows[0].spy_ref == 501.0


# --- the loop (mock clock, no real broker) -----------------------------------


class _Stop(Exception):
    pass


def test_run_daily_arms_once_then_idles():
    close = datetime(2026, 7, 20, 16, 0, tzinfo=EDT)
    nopen = datetime(2026, 7, 21, 9, 30, tzinfo=EDT)
    clocks = iter([
        _clock(True, datetime(2026, 7, 20, 10, 0, tzinfo=EDT), nopen, close),  # arm the session
        _clock(True, datetime(2026, 7, 20, 15, 55, tzinfo=EDT), nopen, close),  # done today -> idle
    ])
    sessions, summaries, begins, sleeps = [], [], [], []

    def sleep(secs):
        sleeps.append(secs)
        raise _Stop  # stop the loop after the first idle sleep

    with pytest.raises(_Stop):
        run_daily(
            clock_fn=lambda: next(clocks),
            session_fn=lambda flatten_at, clock: sessions.append(flatten_at),
            summary_fn=lambda sd, clock: summaries.append(sd),
            begin_run=lambda: begins.append(1),
            sleep=sleep,
        )

    assert sessions == [datetime(2026, 7, 20, 15, 50, tzinfo=EDT)]  # armed exactly once
    assert summaries == [date(2026, 7, 20)]  # rolled up once
    assert begins == [1]  # per-day order cap reset once
    assert len(sleeps) == 1  # then went idle (no second session same day)
