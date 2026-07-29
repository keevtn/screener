"""Scheduled catalysts + fired panel (docs/ROADMAP.md tasks 5b.1, 5b.2, 5b.4).

Scheduled: dated forward catalysts (earnings, M&A dates, PDUFA, lockup expiry) whose
status rolls upcoming → passed on the date. IPO onboarding gives a new listing a
cold-start window and a computed lockup-expiry event. Fired: recently-scored
catalyst clusters, ranked by materiality × recency, with stage/role/evidence.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Entity,
    RawItem,
    ScheduledEvent,
)
from pipeline.common.timeutil import utcnow

LOCKUP_DAYS = 180  # IPO share lockup ≈ 180 calendar days from listing
COLD_START_DAYS = 30


def compute_lockup_expiry(listing_date: date) -> date:
    return listing_date + timedelta(days=LOCKUP_DAYS)


def upsert_scheduled_event(
    session: Session,
    ticker: str,
    catalyst_type: str,
    event_date: date,
    *,
    stage: str | None = "scheduled",
    source: str = "computed",
    meta: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Insert or refresh a scheduled event (idempotent on ticker+type+date)."""
    stmt = sqlite_insert(ScheduledEvent).values(
        ticker=ticker,
        catalyst_type=catalyst_type,
        event_date=event_date,
        stage=stage,
        source=source,
        status="upcoming",
        meta_json=meta or {},
        created_at=now or utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            ScheduledEvent.ticker,
            ScheduledEvent.catalyst_type,
            ScheduledEvent.event_date,
        ],
        set_={
            "stage": stmt.excluded.stage,
            "source": stmt.excluded.source,
            "meta_json": stmt.excluded.meta_json,
        },
    )
    session.execute(stmt)
    session.commit()


def onboard_listing(
    session: Session, ticker: str, listing_date: date, *, now: datetime | None = None
) -> date:
    """New listing: set cold_start_until and create the computed lockup-expiry event."""
    entity = session.get(Entity, ticker)
    cold_until = listing_date + timedelta(days=COLD_START_DAYS)
    if entity is not None:
        entity.cold_start_until = cold_until
        session.commit()
    expiry = compute_lockup_expiry(listing_date)
    upsert_scheduled_event(
        session,
        ticker,
        "lockup_expiry",
        expiry,
        stage="scheduled",
        source="computed",
        meta={"listing_date": listing_date.isoformat()},
        now=now,
    )
    return expiry


def roll_event_status(session: Session, *, now: datetime | None = None) -> int:
    """Roll upcoming events whose date has passed to 'passed' (cancelled untouched)."""
    today = (now or utcnow()).date()
    events = (
        session.execute(select(ScheduledEvent).where(ScheduledEvent.status == "upcoming"))
        .scalars()
        .all()
    )
    n = 0
    for ev in events:
        if ev.event_date < today:
            ev.status = "passed"
            n += 1
    session.commit()
    return n


def scheduled_panel(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Upcoming scheduled catalysts with countdowns (soonest first)."""
    today = (now or utcnow()).date()
    events = (
        session.execute(
            select(ScheduledEvent)
            .where(ScheduledEvent.status == "upcoming")
            .where(ScheduledEvent.event_date >= today)
            .order_by(ScheduledEvent.event_date.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "ticker": ev.ticker,
            "catalyst_type": ev.catalyst_type,
            "event_date": ev.event_date.isoformat(),
            "days_until": (ev.event_date - today).days,
            "stage": ev.stage,
            "source": ev.source,
            "meta": ev.meta_json,
        }
        for ev in events
    ]


def fired_panel(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
    half_life_hours: float | None = None,
    window_days: int = 7,
    order: str = "rank",
) -> list[dict[str, Any]]:
    """Recently-fired catalysts over the last ``window_days`` (default 1 week).

    ``order``:
      * ``"rank"`` (default) — materiality·exp(-age/half_life). The half-life
        **scales to the window** (``half_life_hours`` defaults to ``window_days×24``)
        so materiality dominates and recency is only a gentle decay ACROSS the
        window. This is what makes a 1-week view honest: with a fixed 48h half-life
        a 7-day-old item ranks at exp(-3.5) ≈ 3% of a fresh one — buried under fresh
        minor filings; with a 1-week half-life a week-old MAJOR (mat 0.9 → ×0.37)
        still outranks a fresh MINOR (mat 0.3), so the week's big catalysts surface.
        (At high catalyst volume — thousands/week — newest-first would just show the
        last few hours, so materiality-ranked is the useful default here.)
      * ``"recent"`` — newest-first by publish time (a raw chronological feed).

    Bounded to ``window_days`` via the indexed ``raw_items.published_at`` window +
    one batched entity lookup (no full-history scan, no per-cluster N+1). ``rank``
    is always computed and returned so the UI can re-sort client-side if it wants.
    """
    ref = now or utcnow()
    cutoff = ref - timedelta(days=window_days)
    # Half-life scales to the window so a week view surfaces week-old majors
    # (see docstring); an explicit override still wins.
    half_life = half_life_hours if half_life_hours is not None else max(24.0, window_days * 24.0)
    rows = session.execute(
        select(ClusterScore, Cluster, RawItem)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(ClusterScore.catalyst_type.is_not(None))
        .where(RawItem.published_at >= cutoff)
    ).all()

    # Batch the ticker/role lookup — one query for all candidate clusters instead
    # of one per cluster (the N+1). Chunk the IN() under SQLite's variable limit.
    ents: dict[str, list[dict[str, Any]]] = {}
    cluster_ids = [c.cluster_id for _, c, _ in rows]
    for i in range(0, len(cluster_ids), 900):
        chunk = cluster_ids[i : i + 900]
        for cid, tk, role in session.execute(
            select(ClusterEntity.cluster_id, ClusterEntity.ticker, ClusterEntity.ticker_role).where(
                ClusterEntity.cluster_id.in_(chunk)
            )
        ):
            ents.setdefault(cid, []).append({"ticker": tk, "role": role})

    scored = []
    for cs, cluster, origin in rows:
        age_h = (ref - origin.published_at).total_seconds() / 3600.0
        if age_h < 0:
            continue
        rank = cs.materiality * math.exp(-age_h / half_life)
        scored.append(
            (
                origin.published_at,
                rank,
                {
                    "cluster_id": cluster.cluster_id,
                    "catalyst_type": cs.catalyst_type,
                    "event_stage": cs.event_stage,
                    "materiality": cs.materiality,
                    "high_alert": cs.high_alert,
                    # Two-axis sentiment (I7 — kept separate, never pre-blended):
                    # finbert_score is the primary tone the tape/insight surfaces
                    # render; lm_score is the secondary axis; finbert_label the tag.
                    "finbert_score": cs.finbert_score,
                    "lm_score": cs.lm_score,
                    "finbert_label": cs.finbert_label,
                    "published_at": origin.published_at.isoformat(),
                    # When the system first scored/classified this cluster (the
                    # "call"). Upsert preserves it across re-scores, so it stays
                    # the original call time, not the latest sweep.
                    "called_at": cs.created_at.isoformat() if cs.created_at else None,
                    "title": (origin.payload_json or {}).get("title"),
                    "url": origin.url or (origin.payload_json or {}).get("url"),
                    "tickers": ents.get(cluster.cluster_id, []),
                    "rank": round(rank, 6),
                },
            )
        )
    # order: newest-first (published_at) by default, or curated materiality×recency.
    key = (lambda x: x[1]) if order == "rank" else (lambda x: x[0])
    scored.sort(key=key, reverse=True)
    return [item for _pub, _rank, item in scored[:limit]]
