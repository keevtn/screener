"""Resolve + backfill a prediction's originating-news context (LEDGER lanes).

The prediction ledger is append-only after issue (I4), so a prediction's
origin-news provenance — source_class (the STRUCTURED vs SOCIAL lane the LEDGER
page splits on), headline, url, source — lives in the companion
``prediction_context`` table keyed by prediction_id (see
pipeline.common.models.PredictionContext).

Resolution walks the same evidence a prediction already carries:
  * signal-engine preds:  evidence_json["cluster_ids"]  (contributing clusters)
  * armed-drift preds:    evidence_json["armed_cluster_id"]
  * baseline shadows:     evidence_json["shadows"] -> inherit the shadowed pred's origin

Each cluster resolves through clusters.origin_item_id -> raw_items
(source_class, url, source, title). source_class is 'structured' / 'social' when
the contributing origins agree, else 'mixed'. The primary headline/url is the
first resolvable cluster in cited order.

``backfill_prediction_context`` fills every prediction still missing a context row
in one batched pass. It is used BOTH as the per-cycle arm-time writer (the live DB
carries the cluster family) and — via ``compute_context_rows`` — from
scripts/export_seed.py to build history from the full local DB, whose cluster
family is NOT shipped in the slim seed. Reads only; never mutates predictions.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, Prediction, PredictionContext, RawItem
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.prediction_context")

# Fields a resolved context carries (also the PredictionContext columns filled).
_CTX_FIELDS = ("source_class", "headline", "url", "source", "cluster_id")


def _origin_cluster_ids(evidence: dict[str, Any] | None) -> list[str]:
    """The origin cluster ids a (non-baseline) prediction's evidence points at."""
    if not evidence:
        return []
    cids = evidence.get("cluster_ids")
    if isinstance(cids, list):
        out = [c for c in cids if isinstance(c, str) and c]
        if out:
            return out
    armed = evidence.get("armed_cluster_id")
    if isinstance(armed, str) and armed:
        return [armed]
    return []


def _classify(classes: set[str]) -> str | None:
    if not classes:
        return None
    if classes == {"structured"}:
        return "structured"
    if classes == {"social"}:
        return "social"
    return "mixed"  # contributing origins disagree (rare; kept explicit, not collapsed)


def _cluster_origin_map(
    session: Session, cluster_ids: set[str]
) -> dict[str, tuple[str | None, str | None, str | None, str | None]]:
    """cluster_id -> (source_class, url, source, title), batched to dodge SQLite's
    ~999 bound-variable limit on the IN() list."""
    out: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
    ids = list(cluster_ids)
    CHUNK = 500
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        rows = session.execute(
            select(
                Cluster.cluster_id,
                RawItem.source_class,
                RawItem.url,
                RawItem.source,
                RawItem.payload_json,
            )
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .where(Cluster.cluster_id.in_(chunk))
        ).all()
        for cid, sc, url, src, payload in rows:
            title = payload.get("title") if isinstance(payload, dict) else None
            out[cid] = (sc, url, src, title)
    return out


def _ctx_from_map(
    cluster_ids: list[str],
    origin: dict[str, tuple[str | None, str | None, str | None, str | None]],
) -> dict[str, Any] | None:
    resolvable = [c for c in cluster_ids if c in origin]
    if not resolvable:
        return None
    classes = {origin[c][0] for c in resolvable if origin[c][0] is not None}
    primary = resolvable[0]  # first resolvable in cited (evidence) order
    _, url, src, title = origin[primary]
    return {
        "source_class": _classify(classes),
        "headline": title,
        "url": url,
        "source": src,
        "cluster_id": primary,
    }


def _fetch_context(session: Session, ids: set[str]) -> dict[str, dict[str, Any]]:
    """prediction_id -> ctx for the given ids already in prediction_context (batched).
    Empty when the table is absent (a source DB the seed is built from has none)."""
    out: dict[str, dict[str, Any]] = {}
    wanted = list(ids)
    CHUNK = 500
    try:
        for i in range(0, len(wanted), CHUNK):
            rows = session.execute(
                select(
                    PredictionContext.prediction_id,
                    PredictionContext.source_class,
                    PredictionContext.headline,
                    PredictionContext.url,
                    PredictionContext.source,
                    PredictionContext.cluster_id,
                ).where(PredictionContext.prediction_id.in_(wanted[i : i + CHUNK]))
            ).all()
            for pid, sc, h, u, s, c in rows:
                out[pid] = {"source_class": sc, "headline": h, "url": u, "source": s, "cluster_id": c}
    except OperationalError:
        return {}
    return out


def compute_context_rows(session: Session, *, only_missing: bool = True) -> list[dict[str, Any]]:
    """Resolve context for predictions lacking one; return insertable row dicts.

    Read-only over predictions/clusters/raw_items. ``only_missing`` (the per-cycle
    arm-time path) restricts to predictions with no context row yet via a SQL
    anti-join, so a growing ledger doesn't re-scan every row each cycle.
    """
    stmt = select(Prediction.prediction_id, Prediction.evidence_json)
    if only_missing:
        stmt = stmt.outerjoin(
            PredictionContext, PredictionContext.prediction_id == Prediction.prediction_id
        ).where(PredictionContext.prediction_id.is_(None))
    preds = session.execute(stmt).all()

    directs: list[tuple[str, list[str]]] = []  # (pred_id, origin cluster ids)
    shadows: list[tuple[str, str | None]] = []  # (pred_id, shadowed pred id)
    all_cids: set[str] = set()
    for pid, ev in preds:
        cids = _origin_cluster_ids(ev)
        if cids:
            directs.append((pid, cids))
            all_cids.update(cids)
        else:
            shadow = ev.get("shadows") if isinstance(ev, dict) else None
            shadows.append((pid, shadow if isinstance(shadow, str) and shadow else None))

    origin = _cluster_origin_map(session, all_cids)
    now = utcnow()
    rows: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}  # this run's direct resolutions (for shadow inherit)

    for pid, cids in directs:
        ctx = _ctx_from_map(cids, origin)
        if ctx is not None:
            resolved[pid] = ctx
            rows.append({"prediction_id": pid, "created_at": now, **ctx})

    # Baselines inherit the shadowed prediction's origin — from this run's resolutions,
    # else from an already-persisted context row (a shadow issued in an earlier cycle).
    need = {sh for _, sh in shadows if sh is not None and sh not in resolved}
    persisted = _fetch_context(session, need) if need else {}
    for pid, shadow in shadows:
        if shadow is None:
            continue
        ctx = resolved.get(shadow) or persisted.get(shadow)
        if ctx is not None:
            rows.append(
                {"prediction_id": pid, "created_at": now, **{k: ctx[k] for k in _CTX_FIELDS}}
            )
    return rows


def backfill_prediction_context(session: Session, *, only_missing: bool = True) -> int:
    """Insert context rows for predictions missing one; commit. Best-effort at the
    call site — a resolution failure must never block prediction issuance, so the
    caller wraps this. Returns the number of rows written."""
    rows = compute_context_rows(session, only_missing=only_missing)
    if not rows:
        return 0
    session.bulk_insert_mappings(PredictionContext, rows)
    session.commit()
    return len(rows)
