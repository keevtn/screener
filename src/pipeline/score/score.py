"""Cluster scoring orchestrator (docs/ROADMAP.md Phase 3, tasks 3.1–3.4).

Scores a cluster once, on its origin item's text (I5), across both axes and
persists one cluster_scores row (idempotent upsert). No LLM in the path (I6).
Task 3.4 earnings-surprise guard: for reaction_dependent (earnings) clusters,
guidance language overrides results-level text direction.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterScore, RawItem
from pipeline.common.timeutil import utcnow
from pipeline.enrich.canonical import CanonicalItem, from_raw_item
from pipeline.enrich.tiers import SourceTiers, load_source_tiers
from pipeline.score.catalysts import CatalystTaxonomy, load_taxonomy

log = logging.getLogger("pipeline.score")

# FinBERT inference sub-batch. A whole 256-cluster chunk in ONE onnx call is a
# large tensor (batch × 512 tokens) that can crash/OOM the score step on a small
# instance — the single-text self-test never hits it, so the failure is silent
# and zeroes ALL cluster_scores (observed on Railway 2026-07-30). Running FinBERT
# in small sub-batches bounds peak memory and localizes any failure.
_FINBERT_SUB_BATCH = 16


def _finbert_scores(finbert: Any, pairs: list[tuple[str, str]]) -> list[Any]:
    """FinBERT over pairs in small sub-batches, resilient to a batch-level
    inference failure. A failed sub-batch falls back to None (LM-only) for those
    rows and logs loudly, so the sweep keeps writing cluster_scores instead of
    dying and producing zero scores."""
    if finbert is None:
        return [None] * len(pairs)
    out: list[Any] = []
    for i in range(0, len(pairs), _FINBERT_SUB_BATCH):
        sub = pairs[i : i + _FINBERT_SUB_BATCH]
        try:
            out.extend(finbert.analyze_text_batch(sub))
        except Exception as exc:  # noqa: BLE001 — a bad batch must not zero the sweep
            log.warning(
                "finbert inference failed on %d rows (%r); LM-only for these", len(sub), exc
            )
            out.extend([None] * len(sub))
    return out


def _lm_scores(lm: Any, pairs: list[tuple[str, str]]) -> list[Any]:
    """LM (Loughran–McDonald) over pairs in the same small sub-batches, resilient to a
    batch-level failure. The LM call used to run over the WHOLE 256-cluster chunk
    UNGUARDED — one bad row threw, aborted the chunk before its commit, and because
    ``only_unscored`` re-selects the same clusters next sweep it WEDGED all scoring at
    one batch permanently (observed on Railway: cluster_scores frozen at 256 while
    clusters kept growing). Sub-batching + fallback removes that failure mode."""
    if lm is None:
        return [None] * len(pairs)
    out: list[Any] = []
    for i in range(0, len(pairs), _FINBERT_SUB_BATCH):
        sub = pairs[i : i + _FINBERT_SUB_BATCH]
        try:
            out.extend(lm.analyze_text_batch(sub))
        except Exception as exc:  # noqa: BLE001 — a bad batch must not wedge the sweep
            log.warning("lm inference failed on %d rows (%r); null lm for these", len(sub), exc)
            out.extend([None] * len(sub))
    return out
from pipeline.score.routing import text_kind_of
from pipeline.score.sentiment import SentimentScores, score_sentiment


@dataclass
class ClusterScoreValues:
    cluster_id: str
    finbert_label: str | None
    finbert_score: float | None
    lm_score: float | None
    text_kind: str
    catalyst_type: str | None
    event_stage: str | None
    materiality: float
    direction_hint: str | None
    high_alert: bool
    predictive: bool
    reaction_dependent: bool


def _fallback_score(cluster_id: str, sent: SentimentScores) -> ClusterScoreValues:
    """Minimal row for a cluster whose _assemble_score/persist threw, so only_unscored
    ADVANCES past it instead of re-selecting (and re-wedging) it every sweep. Keeps the
    sentiment we already computed — drops only the catalyst-taxonomy overlay."""
    return ClusterScoreValues(
        cluster_id=cluster_id,
        finbert_label=sent.finbert_label,
        finbert_score=sent.finbert_score,
        lm_score=sent.lm_score,
        text_kind="unknown",
        catalyst_type=None,
        event_stage=None,
        materiality=0.0,
        direction_hint=None,
        high_alert=False,
        predictive=True,
        reaction_dependent=False,
    )


def _assemble_score(
    cluster_id: str,
    origin: CanonicalItem,
    sent: SentimentScores,
    *,
    taxonomy: CatalystTaxonomy,
    tiers: SourceTiers,
) -> ClusterScoreValues:
    """Build the cluster_scores row from precomputed sentiment + the other axes."""
    kind = text_kind_of(origin.source, tiers)
    cat = taxonomy.classify(origin)

    catalyst_type = cat.catalyst_type if cat else None
    event_stage = cat.event_stage if cat else None
    materiality = cat.materiality if cat else 0.0
    direction_hint = cat.direction_hint if cat else None
    high_alert = cat.high_alert if cat else False
    predictive = cat.predictive if cat else True
    reaction_dependent = cat.reaction_dependent if cat else False

    # 3.4 earnings-surprise guard: results-level text ("revenue declined") is level,
    # not surprise. For reaction_dependent clusters, up-weight guidance language —
    # if the text carries a guidance direction, it overrides the text direction;
    # otherwise the cluster stays reaction_dependent for Phase 4 to arm the ticker.
    if reaction_dependent:
        guidance = taxonomy.direction_for("guidance_change", origin.title)  # headline-led
        if guidance:
            direction_hint = guidance

    return ClusterScoreValues(
        cluster_id=cluster_id,
        finbert_label=sent.finbert_label,
        finbert_score=sent.finbert_score,
        lm_score=sent.lm_score,
        text_kind=kind,
        catalyst_type=catalyst_type,
        event_stage=event_stage,
        materiality=materiality,
        direction_hint=direction_hint,
        high_alert=high_alert,
        predictive=predictive,
        reaction_dependent=reaction_dependent,
    )


def score_cluster(
    cluster_id: str,
    origin: CanonicalItem,
    *,
    taxonomy: CatalystTaxonomy,
    tiers: SourceTiers,
    finbert=None,
    lm=None,
) -> ClusterScoreValues:
    sent = score_sentiment(origin.title, origin.description, finbert=finbert, lm=lm)
    return _assemble_score(cluster_id, origin, sent, taxonomy=taxonomy, tiers=tiers)


def persist_cluster_score(session: Session, values: ClusterScoreValues) -> None:
    """Upsert one cluster_scores row (idempotent re-scoring, I5 one-row-per-cluster)."""
    data = asdict(values)
    stmt = sqlite_insert(ClusterScore).values(created_at=utcnow(), **data)
    update = {k: getattr(stmt.excluded, k) for k in data if k != "cluster_id"}
    stmt = stmt.on_conflict_do_update(index_elements=[ClusterScore.cluster_id], set_=update)
    session.execute(stmt)


def score_clusters(
    session: Session,
    *,
    taxonomy: CatalystTaxonomy | None = None,
    tiers: SourceTiers | None = None,
    finbert=None,
    lm=None,
    batch_size: int = 256,
    only_unscored: bool = False,
) -> int:
    """Score clusters on their origin text; returns rows written.

    Sentiment is computed in BATCHES (one analyzer call per chunk, which the
    FinBERT/L-M analyzers batch internally) rather than one forward pass per
    cluster — ~10x faster at archive scale. Commits per chunk (progress + memory).

    ``only_unscored=True`` restricts to clusters with no ClusterScore row yet —
    ALL normal pipeline sweeps use this so fresh clusters score in seconds and
    cost stays O(new), not O(archive). The everything pass (only_unscored=False)
    is reserved for explicit `run_pipeline.py --rescore-all` runs after a
    taxonomy/config change.
    """
    taxonomy = taxonomy or load_taxonomy()
    tiers = tiers or load_source_tiers()
    stmt = select(Cluster)
    if only_unscored:
        stmt = stmt.outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id).where(
            ClusterScore.cluster_id.is_(None)
        )
    # NEWEST cluster first: the tape/feed renders newest-first, and a single sweep may
    # not drain a large unscored backlog — so score what users actually SEE first,
    # instead of the old rowid (oldest-first) order that left recent clusters (the
    # visible tape) unscored behind the archive. Full-rescore passes are unaffected.
    stmt = stmt.order_by(Cluster.created_at.desc())
    clusters = session.execute(stmt).scalars().all()

    rows: list[tuple[str, CanonicalItem]] = []
    for cl in clusters:
        origin_row = session.get(RawItem, cl.origin_item_id)
        if origin_row is not None:
            rows.append((cl.cluster_id, from_raw_item(origin_row)))

    n = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        pairs = [(it.title, it.description) for _, it in chunk]
        fb = _finbert_scores(finbert, pairs)
        lm_r = _lm_scores(lm, pairs)
        for (cluster_id, origin), f, lval in zip(chunk, fb, lm_r, strict=True):
            sent = SentimentScores(
                finbert_label=f.label if f is not None else None,
                finbert_score=f.score if f is not None else None,
                lm_score=lval.score if lval is not None else None,
            )
            # Per-cluster SAVEPOINT: a cluster whose taxonomy/persist throws rolls back
            # JUST itself (not the whole 256-chunk) and still gets a row written, so
            # only_unscored ADVANCES instead of re-selecting and re-wedging it every
            # sweep. This is what unsticks scoring frozen at one batch.
            try:
                with session.begin_nested():
                    persist_cluster_score(
                        session,
                        _assemble_score(cluster_id, origin, sent, taxonomy=taxonomy, tiers=tiers),
                    )
            except Exception as exc:  # noqa: BLE001 — never wedge the sweep on one cluster
                log.warning(
                    "scoring cluster %s failed (%r); writing a sentiment-only row so the "
                    "sweep advances",
                    cluster_id,
                    exc,
                )
                with contextlib.suppress(Exception), session.begin_nested():
                    persist_cluster_score(session, _fallback_score(cluster_id, sent))
            n += 1
        session.commit()
    return n
