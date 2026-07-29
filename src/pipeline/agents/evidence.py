"""Evidence bundle builder (docs/ROADMAP.md task 7.2).

For each candidate ticker, assemble the deterministic bundle the ranker reads:
its recent scored clusters (both axis scores, kept SEPARATE per I7) plus the
ticker's rolling window state (the same composites the signal engine computes).
The bundle is pure-derived and JSON-stable so `test_evidence_bundle_golden` can
pin it. The cluster_ids it lists are the only valid `evidence_ids` a ranking may
cite — that is what makes a ranking auditable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, RawItem
from pipeline.common.timeutil import utcnow
from pipeline.signal.engine import SignalEngine


def _round(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def build_evidence_bundle(
    session: Session,
    ticker: str,
    params: dict[str, Any],
    config_version: str,
    *,
    now: datetime | None = None,
    max_clusters: int = 6,
) -> dict[str, Any]:
    """Deterministic evidence bundle for one candidate ticker."""
    now = now or utcnow()
    window = SignalEngine(session, params, config_version, now=now).build_window(ticker)

    rows = session.execute(
        select(ClusterScore, Cluster, RawItem)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(ClusterEntity.ticker == ticker)
        # Deterministic order: newest first, cluster_id breaks ties (golden-stable).
        .order_by(RawItem.published_at.desc(), Cluster.cluster_id)
        .limit(max_clusters)
    ).all()

    clusters = [
        {
            "cluster_id": cluster.cluster_id,
            "published_at": origin.published_at.isoformat(),
            "source": origin.source,
            "tier": cluster.origin_tier,
            "title": (origin.payload_json or {}).get("title"),
            "text_kind": cs.text_kind,
            "finbert_score": _round(cs.finbert_score),
            "lm_score": _round(cs.lm_score),
            "catalyst_type": cs.catalyst_type,
            "event_stage": cs.event_stage,
            "materiality": _round(cs.materiality),
            "direction_hint": cs.direction_hint,
            "high_alert": cs.high_alert,
        }
        for cs, cluster, origin in rows
    ]

    return {
        "ticker": ticker,
        "as_of": now.isoformat(),
        "config_version": config_version,
        "window": {
            "sentiment_composite": _round(window.sentiment_composite),
            "materiality_composite": _round(window.materiality_composite),
            "item_count": window.item_count,
            "total_weight": _round(window.total_weight),
        },
        "clusters": clusters,
    }
