"""Idempotent enrichment over the archive (docs/ROADMAP.md task 2.5).

INCREMENTAL by default: each pass clusters only raw items not yet in any
cluster, matching them against existing clusters whose origin falls inside the
72h context window (seeds — see cluster.SeedBucket). A full-archive rebuild
(cluster every raw_items row) runs only when the clusters table is empty.

Why: the original whole-archive pass was O(items x active-72h-buckets) EVERY
sweep. At ~10K items per 72h window that is tens of minutes of pure CPU per
sweep — observed live on 2026-07-17 as a pinned core and a starved pipeline
(fresh raw_items, zero new clusters/scores for hours). Incremental cost is
O(new-items x active-window) ≈ seconds.

Re-running stays a no-op on counts: clusters upsert by cluster_id, merged
members dedupe, and each touched cluster's attributions/unmapped rows are
replaced wholesale. Items already in a cluster are never re-clustered, so a
cluster's identity (cluster_id = original origin item) is stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterEntity, RawItem, UnmappedMention
from pipeline.common.timeutil import utcnow
from pipeline.enrich.canonical import CanonicalItem, from_raw_item
from pipeline.enrich.cluster import (
    DEFAULT_WINDOW_HOURS,
    ClusterResult,
    SeedBucket,
    TierFn,
    build_clusters,
    persist_clusters,
)
from pipeline.enrich.resolve import EntityResolver, ResolveResult, resolve_cluster

# Mentions we deliberately decline (policy), not genuine resolution failures.
_POLICY_REASONS = {"blocklist", "common_word"}
# Keep IN() deletes under SQLite's bound-variable limit (default 999).
_DELETE_CHUNK = 500
# Unclustered-candidate fetch bound. The loop runs every ~2 min, so an item
# either clusters within minutes or is a policy skip; 7 days is generous slack
# (e.g. the loop being down over a long weekend).
_INGEST_LOOKBACK_DAYS = 7


@dataclass
class BackfillStats:
    items: int = 0
    clusters: int = 0
    attributions: int = 0
    unmapped: int = 0  # genuine failures (no_match / ambiguous)
    suppressed: int = 0  # deliberate policy declines (blocklist / common_word)

    @property
    def unmapped_rate(self) -> float:
        """Genuine unmapped rate (Gate 2 metric): failures over ticker-bearing mentions.

        Policy declines (cashtag blocklist, common-word names) are intentional and
        excluded — they are correct non-attributions, not resolution failures.
        """
        denom = self.attributions + self.unmapped
        return self.unmapped / denom if denom else 0.0


def backfill_enrichment(
    session: Session,
    *,
    resolver: EntityResolver | None = None,
    tier_of: TierFn | None = None,
) -> BackfillStats:
    if session.execute(select(func.count()).select_from(Cluster)).scalar_one():
        return _incremental(session, resolver=resolver, tier_of=tier_of)
    # First run (empty clusters table): the original whole-archive pass.
    rows = session.execute(select(RawItem)).scalars().all()
    items = [from_raw_item(r) for r in rows]
    results = build_clusters(items, tier_of=tier_of)
    stats = BackfillStats(items=len(items), clusters=len(results))
    _persist_and_resolve(session, results, {it.id: it for it in items}, resolver, stats)
    return stats


def _incremental(
    session: Session,
    *,
    resolver: EntityResolver | None,
    tier_of: TierFn | None,
) -> BackfillStats:
    stats = BackfillStats()
    now = utcnow()
    cutoff = now - timedelta(days=_INGEST_LOOKBACK_DAYS)
    recent = (
        session.execute(select(RawItem).where(RawItem.ingested_at >= cutoff)).scalars().all()
    )
    # Membership is the durable "already enriched" marker — no watermark table.
    known: set[str] = set()
    for cid, mids in session.execute(select(Cluster.cluster_id, Cluster.member_ids_json)):
        known.add(cid)
        known.update(mids or [])
    new_items = [from_raw_item(r) for r in recent if r.id not in known]
    if not new_items:
        return stats
    stats.items = len(new_items)

    # Seeds: existing clusters whose origin published inside the context window —
    # the only clusters a time-ordered full rebuild could merge these items into.
    context_start = min(it.published_at for it in new_items) - timedelta(
        hours=DEFAULT_WINDOW_HOURS
    )
    seeds: list[SeedBucket] = []
    seed_origins: dict[str, CanonicalItem] = {}
    for cl, origin_raw in session.execute(
        select(Cluster, RawItem)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(RawItem.published_at >= context_start)
    ):
        origin = from_raw_item(origin_raw)
        seed_origins[cl.cluster_id] = origin
        seeds.append(
            SeedBucket(
                cluster_id=cl.cluster_id,
                rep_headline=origin.normalized_headline,
                min_pub=origin.published_at,
                member_ids=list(cl.member_ids_json or []),
                origin_tier=cl.origin_tier,
            )
        )

    results = build_clusters(new_items, tier_of=tier_of, seeds=seeds)
    stats.clusters = len(results)
    origin_by_id = {it.id: it for it in new_items} | seed_origins
    _persist_and_resolve(session, results, origin_by_id, resolver, stats)
    return stats


def _persist_and_resolve(
    session: Session,
    results: list[ClusterResult],
    origin_by_id: dict[str, CanonicalItem],
    resolver: EntityResolver | None,
    stats: BackfillStats,
) -> None:
    """Upsert clusters and rebuild attributions/unmapped for exactly those clusters."""
    persist_clusters(session, results)
    cluster_ids = [r.cluster_id for r in results]

    # Replace attributions/unmapped for the re-processed clusters (idempotency).
    # Chunk the IN() delete — a whole-archive backfill has tens of thousands of
    # cluster_ids, far over SQLite's bound-variable limit.
    for i in range(0, len(cluster_ids), _DELETE_CHUNK):
        chunk = cluster_ids[i : i + _DELETE_CHUNK]
        session.execute(delete(ClusterEntity).where(ClusterEntity.cluster_id.in_(chunk)))
        session.execute(delete(UnmappedMention).where(UnmappedMention.cluster_id.in_(chunk)))

    now = utcnow()
    for r in results:
        origin = origin_by_id[r.origin_item_id]
        res = resolve_cluster(origin, resolver) if resolver else ResolveResult()
        for m in res.matches:
            session.add(
                ClusterEntity(
                    cluster_id=r.cluster_id,
                    ticker=m.ticker,
                    ticker_role=m.role,
                    match_method=m.method,
                    created_at=now,
                )
            )
            stats.attributions += 1
        for u in res.unmapped:
            session.add(
                UnmappedMention(
                    cluster_id=r.cluster_id,
                    mention=u.mention,
                    reason=u.reason,
                    created_at=now,
                )
            )
            if u.reason in _POLICY_REASONS:
                stats.suppressed += 1
            else:
                stats.unmapped += 1
    session.commit()
