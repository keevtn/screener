"""Dedup + near-duplicate clustering (docs/ROADMAP.md task 2.2).

Exact-headline pass folds verbatim syndication; then near-duplicate clustering via
``rapidfuzz.fuzz.token_set_ratio ≥ 90`` on normalized headlines within a 72h
window. One story → one cluster → one scoring target (I5). Cluster origin =
earliest ``published_at``; source tier breaks ties (task 2.3, injected).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz, process
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster
from pipeline.common.timeutil import utcnow
from pipeline.enrich.canonical import CanonicalItem

DEFAULT_THRESHOLD = 90
DEFAULT_WINDOW_HOURS = 72

TierFn = Callable[[str], int]


@dataclass
class ClusterResult:
    origin_item_id: str
    member_ids: list[str]
    origin_tier: int | None
    created_at: datetime

    @property
    def cluster_id(self) -> str:
        return self.origin_item_id


@dataclass
class SeedBucket:
    """An existing persisted cluster, offered as a merge target for NEW items
    (incremental enrichment). rep/min_pub come from the cluster's origin item;
    member_ids are its current members. cluster_id stays stable on merge."""

    cluster_id: str
    rep_headline: str
    min_pub: datetime
    member_ids: list[str]
    origin_tier: int | None = None


@dataclass
class _Bucket:
    rep_headline: str
    members: list[CanonicalItem] = field(default_factory=list)
    min_pub: datetime | None = None
    seed: SeedBucket | None = None  # set when this bucket wraps an existing cluster


def build_clusters(
    items: Iterable[CanonicalItem],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    tier_of: TierFn | None = None,
    now: datetime | None = None,
    seeds: Iterable[SeedBucket] | None = None,
) -> list[ClusterResult]:
    """Group canonical items into story clusters (deterministic, time-ordered).

    With ``seeds`` (incremental mode), existing clusters join the active scan as
    merge targets: a new item matching a seed folds into that cluster (stable
    cluster_id, origin preserved); only NEW and CHANGED clusters are returned —
    untouched seeds are omitted so callers persist nothing for them.
    """
    created_at = now or utcnow()
    ordered = sorted(items, key=lambda it: it.published_at)
    window_seconds = window_hours * 3600
    # Items are time-ordered, so once an item passes a bucket's 72h window that
    # bucket can never match a later item — retire it from the active scan. This
    # keeps matching O(items × active-window) instead of O(items × all-buckets),
    # which matters at archive scale (tens of thousands of items).
    active: list[_Bucket] = []
    active_headlines: list[str] = []  # parallel to active, for the C-level matcher
    finalized: list[_Bucket] = []
    # Seeds enter chronologically (same order a full rebuild would have created
    # them), so first-match-wins ties resolve identically to a full pass.
    for sb in sorted(seeds or [], key=lambda s: s.min_pub):
        active.append(_Bucket(rep_headline=sb.rep_headline, min_pub=sb.min_pub, seed=sb))
        active_headlines.append(sb.rep_headline)

    for it in ordered:
        headline = it.normalized_headline
        if active:
            still_a: list[_Bucket] = []
            still_h: list[str] = []
            for bucket, rep in zip(active, active_headlines, strict=True):
                assert bucket.min_pub is not None
                if (it.published_at - bucket.min_pub).total_seconds() > window_seconds:
                    finalized.append(bucket)  # window closed
                else:
                    still_a.append(bucket)
                    still_h.append(rep)
            active, active_headlines = still_a, still_h

        placed: _Bucket | None = None
        if headline and active:
            # Match against the active window in one C call (rapidfuzz process);
            # on ties extractOne returns the lowest index = earliest bucket, so
            # "first match wins" is preserved.
            match = process.extractOne(
                headline, active_headlines, scorer=fuzz.token_set_ratio, score_cutoff=threshold
            )
            if match is not None:
                placed = active[match[2]]
        if placed is None:
            placed = _Bucket(rep_headline=headline, min_pub=it.published_at)
            active.append(placed)
            active_headlines.append(headline)
        placed.members.append(it)

    buckets = finalized + active

    def _tier(source: str) -> int:
        return tier_of(source) if tier_of else 0

    results: list[ClusterResult] = []
    for bucket in buckets:
        if bucket.seed is not None:
            if not bucket.members:
                continue  # existing cluster untouched this pass — nothing to persist
            known = set(bucket.seed.member_ids)
            merged = bucket.seed.member_ids + [m.id for m in bucket.members if m.id not in known]
            results.append(
                ClusterResult(
                    origin_item_id=bucket.seed.cluster_id,  # stable identity (PK)
                    member_ids=merged,
                    origin_tier=bucket.seed.origin_tier,
                    created_at=created_at,
                )
            )
            continue
        # Origin: earliest published_at; lower source tier breaks ties (2.3).
        origin = min(bucket.members, key=lambda it: (it.published_at, _tier(it.source)))
        results.append(
            ClusterResult(
                origin_item_id=origin.id,
                member_ids=[m.id for m in bucket.members],
                origin_tier=_tier(origin.source) if tier_of else None,
                created_at=created_at,
            )
        )
    return results


def persist_clusters(session: Session, results: Iterable[ClusterResult]) -> int:
    """Upsert clusters by cluster_id (idempotent for backfill re-runs, task 2.5)."""
    n = 0
    for r in results:
        stmt = sqlite_insert(Cluster).values(
            cluster_id=r.cluster_id,
            origin_item_id=r.origin_item_id,
            member_ids_json=r.member_ids,
            origin_tier=r.origin_tier,
            member_count=len(r.member_ids),
            created_at=r.created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Cluster.cluster_id],
            set_={
                "origin_item_id": stmt.excluded.origin_item_id,
                "member_ids_json": stmt.excluded.member_ids_json,
                "origin_tier": stmt.excluded.origin_tier,
                "member_count": stmt.excluded.member_count,
            },
        )
        session.execute(stmt)
        n += 1
    session.commit()
    return n
