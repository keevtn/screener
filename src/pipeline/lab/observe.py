"""Write signal-lab observations at scoring time (docs/ROADMAP.md task 5c.1).

One row per ticker-attributed scored cluster, with the features logged now and
analyzed whenever. Point-in-time fundamentals join the nearest snapshot at or
before t0 — never after (I12). Backfilled (legacy-import) observations are flagged
and keep null fundamentals. Idempotent per (cluster, ticker).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    RawItem,
    SignalObservation,
)
from pipeline.common.timeutil import utcnow

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)
DEFAULT_NOVELTY_WINDOW_DAYS = 30
LEGACY_ORIGIN = "legacy_import_v1"


def observation_id(cluster_id: str, ticker: str) -> str:
    return hashlib.sha256(f"{cluster_id}|{ticker}".encode()).hexdigest()[:40]


def _after_hours(t0: datetime) -> bool:
    local = t0.astimezone(_ET)
    return local.weekday() >= 5 or not (_MARKET_OPEN <= local.time() < _MARKET_CLOSE)


def _cap_bucket(market_cap: float | None) -> str | None:
    if market_cap is None:
        return None
    b = market_cap
    if b >= 200e9:
        return "mega"
    if b >= 10e9:
        return "large"
    if b >= 2e9:
        return "mid"
    if b >= 300e6:
        return "small"
    return "micro"


def _fundamentals_at(session: Session, ticker: str, t0: datetime) -> FundamentalsSnapshot | None:
    """Nearest fundamentals snapshot at or before t0 (I12: never after)."""
    return session.execute(
        select(FundamentalsSnapshot)
        .where(FundamentalsSnapshot.ticker == ticker)
        .where(FundamentalsSnapshot.as_of <= t0.date())
        .order_by(FundamentalsSnapshot.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()


def observe_scored_clusters(
    session: Session,
    *,
    novelty_window_days: int = DEFAULT_NOVELTY_WINDOW_DAYS,
    only_new: bool = True,
) -> int:
    """Write an observation per ticker-attributed scored cluster.

    INCREMENTAL by default (only_new): only (cluster, ticker) pairs without an
    observation yet are processed — like score_clusters(only_unscored). The old
    behavior re-observed the ENTIRE archive on every 2-minute fast sweep (an
    N+1 fundamentals SELECT per row + an O(k²) novelty pass per ticker + a full
    re-upsert), unbounded as the archive grows — the same stall class already
    fixed for clustering and marking. Safe because novelty_rank counts a
    ticker's observations BACKWARD within a trailing window, so a new (later)
    cluster never changes an existing observation's rank. only_new=False keeps
    the full recompute for a historical/backfill import, where out-of-time-order
    inserts DO require re-ranking across the whole set.
    """
    q = (
        select(ClusterScore, Cluster, ClusterEntity, RawItem)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
    )
    if only_new:
        q = q.outerjoin(
            SignalObservation,
            and_(
                SignalObservation.cluster_id == Cluster.cluster_id,
                SignalObservation.ticker == ClusterEntity.ticker,
            ),
        ).where(SignalObservation.observation_id.is_(None))
    rows = session.execute(q).all()

    built: list[dict[str, Any]] = []
    for score, cluster, entity, origin in rows:
        backfill = bool((origin.payload_json or {}).get("origin") == LEGACY_ORIGIN)
        feats: dict[str, Any] = {
            "finbert_score": score.finbert_score,
            "lm_score": score.lm_score,
            "catalyst_type": score.catalyst_type,
            "event_stage": score.event_stage,
            "materiality": score.materiality,
            "high_alert": score.high_alert,
            "direction_hint": score.direction_hint,
            "reaction_dependent": score.reaction_dependent,
            "source_tier": cluster.origin_tier,
            "text_kind": score.text_kind,
            "ticker_role": entity.ticker_role,
            "after_hours": _after_hours(origin.published_at),
            "days_to_earnings": None,
        }
        if not backfill:
            fund = _fundamentals_at(session, entity.ticker, origin.published_at)
            if fund is not None:
                feats.update(
                    cap_bucket=_cap_bucket(fund.market_cap),
                    short_float=fund.short_float,
                    shares_float=fund.shares_float,
                    insider_own=fund.insider_own,
                    inst_own=fund.inst_own,
                    beta=fund.beta,
                    sector=fund.sector,
                )
        built.append(
            {
                "observation_id": observation_id(cluster.cluster_id, entity.ticker),
                "cluster_id": cluster.cluster_id,
                "ticker": entity.ticker,
                "t0": origin.published_at,
                "features_json": feats,
                "backfill": backfill,
            }
        )

    # novelty_rank: how many of the ticker's observations fall in [t0-window, t0]
    # (itself included). For the incremental path we must count EXISTING DB
    # neighbors too, not just this batch — so a new cluster's rank reflects the
    # ticker's real recent history, matching the full-recompute result.
    window_td = timedelta(days=novelty_window_days)
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in built:
        by_ticker[b["ticker"]].append(b)
    prior: dict[str, list[datetime]] = defaultdict(list)
    if only_new and built:
        earliest = min(b["t0"] for b in built) - window_td
        for tk, t0 in session.execute(
            select(SignalObservation.ticker, SignalObservation.t0)
            .where(SignalObservation.ticker.in_(list(by_ticker)))
            .where(SignalObservation.t0 >= earliest)
        ):
            prior[tk].append(t0)
    for tk, obs_list in by_ticker.items():
        obs_list.sort(key=lambda b: b["t0"])
        universe = sorted(prior[tk] + [b["t0"] for b in obs_list])
        for b in obs_list:
            b["novelty_rank"] = sum(
                1 for t in universe if t <= b["t0"] and (b["t0"] - t) <= window_td
            )

    now = utcnow()
    for b in built:
        stmt = sqlite_insert(SignalObservation).values(
            observation_id=b["observation_id"],
            cluster_id=b["cluster_id"],
            ticker=b["ticker"],
            t0=b["t0"],
            features_json=b["features_json"],
            marks_json={},
            clean_window=None,
            novelty_rank=b["novelty_rank"],
            backfill=b["backfill"],
            status="open",
            created_at=now,
        )
        # Idempotent: refresh features/novelty but never clobber marks/status.
        stmt = stmt.on_conflict_do_update(
            index_elements=[SignalObservation.observation_id],
            set_={
                "features_json": stmt.excluded.features_json,
                "novelty_rank": stmt.excluded.novelty_rank,
                "backfill": stmt.excluded.backfill,
            },
        )
        session.execute(stmt)
    session.commit()
    return len(built)
