"""News archive — browse a past day's newsfeed from the SQLite raw_items plane.

The LIVE tape reads the shallow Mongo /api/news window; this reads OUR full-history
raw_items (the plane with attribution) day by day, so any past session's news is
browsable exactly like the live tape. Each row is a raw_item, enriched from the
cluster it ORIGINATES (tickers via cluster_entities, sentiment/catalyst via
cluster_scores) — the same display fields the tape shows. Day-bounded on
``published_at`` in ET, paginated (busy days run 10k+ items), with the tape's
ticker/source/headline filters applied server-side so pagination counts are honest.

Enrichment is via ``clusters.origin_item_id`` (indexed) — near-duplicate MEMBER
items render with time/source/title/url but no ticker/sentiment overlay (the story's
attribution lives on its origin). Honest, and cheap.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, RawItem
from pipeline.common.timeutil import utcnow

ET = ZoneInfo("America/New_York")
_DATES_LOOKBACK_DAYS = 120  # bound the calendar range (and the distinct-date scan)


def _day_bounds_utc(day: date_) -> tuple[datetime, datetime]:
    """[day 00:00 ET, next day 00:00 ET) in UTC — DST-correct via ZoneInfo."""
    start = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(ZoneInfo("UTC"))
    return start, start + timedelta(days=1)


def _source_type(source: str | None, source_class: str) -> str:
    """Coarse tape SRC tag from the raw_item — social lane, or sec/fda/rss inferred
    from the structured source name (the payload doesn't carry the finer type)."""
    if source_class == "social":
        return "social"
    s = (source or "").lower()
    if "sec" in s or "edgar" in s:
        return "sec"
    if "fda" in s:
        return "fda"
    return "rss"


def _enrich_news_items(session: Session, rows: list[RawItem]) -> list[dict[str, Any]]:
    """Turn a page of raw_items into tape items, attributed to the cluster they
    originate (batched sentiment + tickers, no N+1). Shared by the archive and the
    LIVE feed so both emit the identical per-item shape the frontend expects."""
    ids = [r.id for r in rows]
    cluster_of: dict[str, str] = {}
    if ids:
        for origin_id, cluster_id in session.execute(
            select(Cluster.origin_item_id, Cluster.cluster_id).where(
                Cluster.origin_item_id.in_(ids)
            )
        ):
            cluster_of[origin_id] = cluster_id
    cluster_ids = list(cluster_of.values())
    scores: dict[str, ClusterScore] = {}
    tickers_by_cluster: dict[str, list[str]] = {}
    if cluster_ids:
        for cs in session.execute(
            select(ClusterScore).where(ClusterScore.cluster_id.in_(cluster_ids))
        ).scalars():
            scores[cs.cluster_id] = cs
        for cid, tk in session.execute(
            select(ClusterEntity.cluster_id, ClusterEntity.ticker).where(
                ClusterEntity.cluster_id.in_(cluster_ids)
            )
        ):
            tickers_by_cluster.setdefault(cid, []).append(tk)

    items = []
    for r in rows:
        cid = cluster_of.get(r.id)
        cs = scores.get(cid) if cid else None
        payload = r.payload_json or {}
        items.append(
            {
                "id": r.id,
                "published_at": r.published_at.isoformat(),
                "source": r.source,
                "source_type": _source_type(r.source, r.source_class),
                "title": payload.get("title") or "",
                "url": r.url or payload.get("url"),
                "tickers": sorted(set(tickers_by_cluster.get(cid, []))) if cid else [],
                "sentiment": (
                    {"score": cs.finbert_score} if cs and cs.finbert_score is not None else None
                ),
                "catalyst_type": cs.catalyst_type if cs else None,
                "high_alert": bool(cs.high_alert) if cs else False,
            }
        )
    return items


def live_news(
    session: Session,
    *,
    source_type: str | None = None,
    ticker: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Most-recent news across all days from raw_items, newest first — the deploy
    LIVE feed that replaces the external Mongo middleware. Same per-item shape and
    cluster attribution as :func:`news_archive`, but unbounded by ET day and keyed
    off the frontend's coarse ``source_type`` (rss|sec|fda|social) instead of lane.

    ``source_type='social'`` maps to the social lane in SQL; sec/fda/rss are derived
    from the source name (the payload lacks the finer type), so those are tagged then
    filtered in Python — we over-fetch to keep the page full. ``ticker`` restricts to
    items whose origin-cluster is attributed to it (same subquery as the archive)."""
    fine = source_type in ("sec", "fda", "rss")
    conds = []
    if source_type == "social":
        conds.append(RawItem.source_class == "social")
    elif fine:
        conds.append(RawItem.source_class == "structured")
    if ticker:
        conds.append(
            RawItem.id.in_(
                select(Cluster.origin_item_id)
                .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
                .where(ClusterEntity.ticker == ticker.upper())
            )
        )
    fetch = limit * 4 if fine else limit  # over-fetch, then Python-filter the fine tag
    rows = (
        session.execute(
            select(RawItem).where(*conds).order_by(RawItem.published_at.desc()).limit(fetch)
        )
        .scalars()
        .all()
    )
    items = _enrich_news_items(session, rows)
    if fine:
        items = [it for it in items if it["source_type"] == source_type][:limit]
    return {"count": len(items), "limit": limit, "items": items}


def news_archive(
    session: Session,
    *,
    day: date_,
    lane: str | None = None,
    ticker: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 300,
    offset: int = 0,
) -> dict[str, Any]:
    """One ET day's news from raw_items, newest first, paginated + filtered.

    ``lane`` = source_class ('structured'|'social'); ``ticker`` restricts to items
    whose origin-cluster is attributed to it; ``source`` = exact feed name; ``q`` =
    headline substring. count is the full filtered total for the day (for paging)."""
    start, end = _day_bounds_utc(day)
    conds = [RawItem.published_at >= start, RawItem.published_at < end]
    if lane:
        conds.append(RawItem.source_class == lane)
    if source:
        conds.append(RawItem.source == source)
    if q:
        conds.append(func.json_extract(RawItem.payload_json, "$.title").like(f"%{q}%"))
    if ticker:
        conds.append(
            RawItem.id.in_(
                select(Cluster.origin_item_id)
                .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
                .where(ClusterEntity.ticker == ticker.upper())
            )
        )

    total = session.execute(select(func.count()).select_from(RawItem).where(*conds)).scalar_one()
    rows = (
        session.execute(
            select(RawItem)
            .where(*conds)
            .order_by(RawItem.published_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    items = _enrich_news_items(session, rows)
    return {
        "date": day.isoformat(),
        "lane": lane,
        "count": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def news_archive_dates(session: Session, *, now: datetime | None = None) -> list[date_]:
    """ET dates that have news, newest first — the calendar's navigable range.

    Bounded to the last ``_DATES_LOOKBACK_DAYS`` so the distinct scan stays cheap and
    stray back-dated filings don't blow the range out. A gap day (none ingested) is
    simply absent — the calendar disables it."""
    now = now or utcnow()
    today = now.astimezone(ET).date()
    floor_utc = _day_bounds_utc(today - timedelta(days=_DATES_LOOKBACK_DAYS))[0]
    # Group by ET date via the -4h EDT approximation (the app's July-EDT convention;
    # a DST-exact GROUP BY would need a tz the SQLite build lacks). Correct all summer.
    rows = session.execute(
        select(func.date(func.datetime(RawItem.published_at, "-4 hours")).label("d"))
        .where(RawItem.published_at >= floor_utc)
        .group_by("d")
        .order_by(func.min(RawItem.published_at).desc())
    ).all()
    out: list[date_] = []
    for (d,) in rows:
        try:
            out.append(date_.fromisoformat(d))
        except (TypeError, ValueError):
            continue
    return out


def date_label(d: date_) -> str:
    """'Wed Jul 16' — weekday + month + day, cross-platform (no %-d/%#d)."""
    return f"{d.strftime('%a %b')} {d.day}"
