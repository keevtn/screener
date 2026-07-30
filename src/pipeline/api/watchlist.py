"""TRADER watchlist lane (Phase 3) — user-pinned tickers wired into local state.

Pins live in OUR DB (``watchlist_pins``), not Alpaca's watchlist store, so each
pinned ticker can carry the context Alpaca's flat symbol list can't: an
'armed — waiting for catalyst' read from armed_states / scheduled_events, a buzz
z-score, the latest premarket move, and the most recent catalyst headline.

View/stage only: this module reads/writes OUR watchlist table and never touches
Alpaca or places an order. All enrichment is batch-queried over the (small) pin
set — no per-ticker N+1 — and every lookup is fail-soft so a missing rollup table
degrades to null rather than 500ing the lane.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pipeline.aggregate.attention import buzz_z
from pipeline.api.trader import _cluster_context
from pipeline.common.models import (
    ArmedState,
    AttentionDaily,
    BuzzBaseline,
    ClusterEntity,
    ExtendedSessionDaily,
    ScheduledEvent,
    WatchlistPin,
)
from pipeline.common.timeutil import utcnow


def ensure_watchlist_table(engine: Engine) -> None:
    """Create watchlist_pins if it doesn't exist yet (idempotent). The long-lived
    prod DB predates this table; create_all only runs at init, so make the API
    self-heal on boot the way ensure_indexes does."""
    try:
        WatchlistPin.__table__.create(bind=engine, checkfirst=True)
    except Exception:  # noqa: BLE001 — never block app startup on this
        pass


def list_pins(session: Session) -> list[WatchlistPin]:
    return list(
        session.execute(select(WatchlistPin).order_by(WatchlistPin.created_at.desc()))
        .scalars()
        .all()
    )


def add_pin(session: Session, ticker: str, note: str | None = None) -> WatchlistPin:
    """Idempotent pin. Re-pinning an existing ticker just updates its note."""
    tk = ticker.strip().upper()
    if not tk:
        raise ValueError("ticker required")
    existing = session.get(WatchlistPin, tk)
    if existing is not None:
        if note is not None:
            existing.note = note
        session.commit()
        return existing
    pin = WatchlistPin(ticker=tk, created_at=utcnow(), note=note, source="user")
    session.add(pin)
    session.commit()
    return pin


def remove_pin(session: Session, ticker: str) -> bool:
    pin = session.get(WatchlistPin, ticker.strip().upper())
    if pin is None:
        return False
    session.delete(pin)
    session.commit()
    return True


# --------------------------------------------------------------------------- #
# enrichment
# --------------------------------------------------------------------------- #
def _latest_by_ticker(rows: list[Any], key: str = "ticker") -> dict[str, Any]:
    """Reduce a pre-ordered row list to the FIRST row per ticker (callers pass a
    query ordered so 'first' means 'latest'/'earliest' as intended)."""
    out: dict[str, Any] = {}
    for r in rows:
        tk = getattr(r, key).upper()
        out.setdefault(tk, r)
    return out


def _armed_state(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    rows = (
        session.execute(
            select(ArmedState)
            .where(ArmedState.ticker.in_(tickers), ArmedState.status == "armed")
            .order_by(ArmedState.armed_at.desc())
        )
        .scalars()
        .all()
    )
    latest = _latest_by_ticker(rows)
    return {
        tk: {"catalyst_type": a.catalyst_type, "armed_at": a.armed_at.isoformat(), "cluster_id": a.cluster_id}
        for tk, a in latest.items()
    }


def _scheduled(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    today = utcnow().date()
    rows = (
        session.execute(
            select(ScheduledEvent)
            .where(
                ScheduledEvent.ticker.in_(tickers),
                ScheduledEvent.status == "upcoming",
                ScheduledEvent.event_date >= today,
            )
            .order_by(ScheduledEvent.event_date.asc())
        )
        .scalars()
        .all()
    )
    # earliest upcoming per ticker (query is asc, so first is earliest)
    out: dict[str, dict[str, Any]] = {}
    for e in rows:
        tk = e.ticker.upper()
        if tk in out:
            continue
        out[tk] = {
            "catalyst_type": e.catalyst_type,
            "event_date": e.event_date.isoformat() if hasattr(e.event_date, "isoformat") else str(e.event_date),
            "stage": e.stage,
        }
    return out


def _premarket(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    try:
        rows = (
            session.execute(
                select(ExtendedSessionDaily)
                .where(ExtendedSessionDaily.ticker.in_(tickers))
                .order_by(ExtendedSessionDaily.session_date.desc())
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001
        return {}
    latest = _latest_by_ticker(rows)
    return {
        tk: {
            "session_date": r.session_date.isoformat() if hasattr(r.session_date, "isoformat") else str(r.session_date),
            "pm_pct": r.pm_pct,
            "pm_last": r.pm_last,
            "reg_pct": r.reg_pct,
            "prior_close": r.prior_close,
        }
        for tk, r in latest.items()
    }


def _buzz(session: Session, tickers: list[str]) -> dict[str, float]:
    try:
        baselines = {
            b.ticker: b
            for b in session.execute(
                select(BuzzBaseline).where(BuzzBaseline.ticker.in_(tickers))
            ).scalars()
        }
        if not baselines:
            return {}
        rows = (
            session.execute(
                select(AttentionDaily)
                .where(AttentionDaily.ticker.in_(tickers))
                .order_by(AttentionDaily.date.desc())
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001
        return {}
    latest = _latest_by_ticker(rows)
    out: dict[str, float] = {}
    for tk, a in latest.items():
        z = buzz_z(a.social_count, baselines.get(tk))
        if z is not None:
            out[tk] = round(z, 2)
    return out


def _latest_catalyst(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    rows = (
        session.execute(
            select(ClusterEntity)
            .where(ClusterEntity.ticker.in_(tickers))
            .order_by(ClusterEntity.created_at.desc())
        )
        .scalars()
        .all()
    )
    latest = _latest_by_ticker(rows)
    ctx = _cluster_context(session, [ce.cluster_id for ce in latest.values()])
    out: dict[str, dict[str, Any]] = {}
    for tk, ce in latest.items():
        c = ctx.get(ce.cluster_id)
        if c:
            out[tk] = {**c, "cluster_id": ce.cluster_id}
    return out


def watchlist_view(session: Session) -> dict[str, Any]:
    """Enriched pin set for the watchlist lane. Each pin carries its armed/
    scheduled catalyst state, buzz z, latest premarket move, and most-recent
    catalyst headline. State: armed (PEAD reaction pending) > scheduled (upcoming
    dated catalyst) > watching."""
    pins = list_pins(session)
    tickers = [p.ticker for p in pins]
    if not tickers:
        return {"count": 0, "items": []}

    armed = _armed_state(session, tickers)
    scheduled = _scheduled(session, tickers)
    premkt = _premarket(session, tickers)
    buzz = _buzz(session, tickers)
    catalyst = _latest_catalyst(session, tickers)

    items: list[dict[str, Any]] = []
    for p in pins:
        tk = p.ticker
        a = armed.get(tk)
        sch = scheduled.get(tk)
        if a is not None:
            state = "armed"
            state_label = f"armed — waiting on {a['catalyst_type']} reaction"
        elif sch is not None:
            state = "scheduled"
            state_label = f"{sch['catalyst_type']} scheduled {sch['event_date']}"
        else:
            state = "watching"
            state_label = "watching"
        items.append(
            {
                "ticker": tk,
                "created_at": p.created_at.isoformat(),
                "note": p.note,
                "state": state,
                "state_label": state_label,
                "armed": a,
                "scheduled": sch,
                "premarket": premkt.get(tk),
                "buzz_z": buzz.get(tk),
                "catalyst": catalyst.get(tk),
            }
        )
    return {"count": len(items), "items": items}
