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
in one batched pass, then (a) writes an all-null SENTINEL for aged predictions whose
origin genuinely can't resolve — so they stop being retried forever and show an
honest "—" — and (b) REPAIRS any null-field row whose evidence now resolves. Used
BOTH as the per-cycle arm-time writer (the live DB carries the cluster family) and —
via ``compute_context_rows`` — from tests. Reads predictions/clusters/raw_items only;
never mutates predictions.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, Prediction, PredictionContext, RawItem
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.prediction_context")

# Fields a resolved context carries (also the PredictionContext columns filled).
_CTX_FIELDS = ("source_class", "headline", "url", "source", "cluster_id")

# A prediction whose origin can't resolve is only SENTINELed (marked resolved-empty)
# once it's older than this — a fresh arm can be transiently unresolvable (its cluster
# committed the same cycle) and deserves a few retries before we mark it "—".
_SENTINEL_AGE = timedelta(minutes=10)

_CHUNK = 500  # stay under SQLite's ~999 bound-variable limit on IN() lists


def _coerce_evidence(ev: Any) -> dict[str, Any]:
    """Normalize a stored evidence_json to a dict.

    It SHOULD already be a dict, but a double-encoded row (JSON serialized twice ->
    the column holds a JSON *string*) reads back as ``str``; a plain isinstance-dict
    guard then silently drops it (no cluster_ids extracted, no row, no log — the
    2026-07-30 "new ledgers show no origin" bug). Parse a string once so those rows
    still resolve. Anything that isn't a dict or a JSON object becomes {}."""
    if isinstance(ev, dict):
        return ev
    if isinstance(ev, str):
        try:
            parsed = json.loads(ev)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _origin_cluster_ids(evidence: dict[str, Any]) -> list[str]:
    """The origin cluster ids a (non-baseline) prediction's evidence points at.
    Assumes evidence is already coerced to a dict (see _coerce_evidence)."""
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
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
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
    try:
        for i in range(0, len(wanted), _CHUNK):
            rows = session.execute(
                select(
                    PredictionContext.prediction_id,
                    PredictionContext.source_class,
                    PredictionContext.headline,
                    PredictionContext.url,
                    PredictionContext.source,
                    PredictionContext.cluster_id,
                ).where(PredictionContext.prediction_id.in_(wanted[i : i + _CHUNK]))
            ).all()
            for pid, sc, h, u, s, c in rows:
                out[pid] = {"source_class": sc, "headline": h, "url": u, "source": s, "cluster_id": c}
    except OperationalError:
        return {}
    return out


def _sentinel_row(pid: str, cluster_id: str | None, now: Any) -> dict[str, Any]:
    """An all-null context row: 'resolution attempted, origin unavailable'. source_class
    IS NULL is the marker that distinguishes it from a real resolution (which always
    carries a non-null source_class) and is what the repair pass re-resolves."""
    return {
        "prediction_id": pid,
        "created_at": now,
        "source_class": None,
        "headline": None,
        "url": None,
        "source": None,
        "cluster_id": cluster_id,
    }


def compute_context_rows(session: Session, *, only_missing: bool = True) -> list[dict[str, Any]]:
    """Resolve context for predictions lacking one; return insertable row dicts.

    Read-only over predictions/clusters/raw_items. ``only_missing`` (the per-cycle
    arm-time path) restricts to predictions with no context row yet via a SQL
    anti-join, so a growing ledger doesn't re-scan every row each cycle. An aged
    prediction whose origin can't resolve gets an all-null SENTINEL row so it stops
    being selected as missing forever.
    """
    stmt = select(Prediction.prediction_id, Prediction.evidence_json, Prediction.issued_at)
    if only_missing:
        stmt = stmt.outerjoin(
            PredictionContext, PredictionContext.prediction_id == Prediction.prediction_id
        ).where(PredictionContext.prediction_id.is_(None))
    preds = session.execute(stmt).all()

    directs: list[tuple[str, list[str], Any]] = []  # (pred_id, origin cluster ids, issued_at)
    shadows: list[tuple[str, str | None]] = []  # (pred_id, shadowed pred id)
    all_cids: set[str] = set()
    skipped = 0
    for pid, ev_raw, issued_at in preds:
        # One malformed row must never abort the batch (which would starve every
        # other missing prediction of context, silently, every cycle).
        try:
            ev = _coerce_evidence(ev_raw)
            cids = _origin_cluster_ids(ev)
            if cids:
                directs.append((pid, cids, issued_at))
                all_cids.update(cids)
            else:
                shadow = ev.get("shadows")
                shadows.append((pid, shadow if isinstance(shadow, str) and shadow else None))
        except Exception:  # noqa: BLE001 — resilience: skip the bad row, keep going
            skipped += 1
            log.warning("prediction_context: skipping unresolvable evidence for %s", pid)
    if skipped:
        log.warning("prediction_context: %d prediction(s) had unusable evidence", skipped)

    origin = _cluster_origin_map(session, all_cids)
    now = utcnow()
    rows: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}  # this run's direct resolutions (for shadow inherit)
    sentinels = 0

    for pid, cids, issued_at in directs:
        ctx = _ctx_from_map(cids, origin)
        if ctx is not None:
            resolved[pid] = ctx
            rows.append({"prediction_id": pid, "created_at": now, **ctx})
        elif only_missing and issued_at is not None and (now - issued_at) > _SENTINEL_AGE:
            # Aged + origin genuinely unresolvable -> mark resolved-empty so it stops
            # looping. (Repaired later if the cluster ever becomes resolvable.)
            rows.append(_sentinel_row(pid, cids[0] if cids else None, now))
            sentinels += 1
    if sentinels:
        log.info("prediction_context: sentineled %d aged unresolvable prediction(s)", sentinels)

    # Baselines inherit the shadowed prediction's origin — from this run's resolutions,
    # else from an already-persisted context row (a shadow issued in an earlier cycle).
    need = {sh for _, sh in shadows if sh is not None and sh not in resolved}
    persisted = _fetch_context(session, need) if need else {}
    for pid, shadow in shadows:
        if shadow is None:
            continue
        ctx = resolved.get(shadow) or persisted.get(shadow)
        # Don't propagate a sentinel's null fields as if they were a resolution.
        if ctx is not None and ctx.get("source_class") is not None:
            rows.append(
                {"prediction_id": pid, "created_at": now, **{k: ctx[k] for k in _CTX_FIELDS}}
            )
    return rows


def _insert_rows(session: Session, rows: list[dict[str, Any]]) -> int:
    """Bulk-insert context rows, degrading to per-row so one bad row can't abort the
    batch (which would silently starve every new prediction of context)."""
    if not rows:
        return 0
    try:
        session.bulk_insert_mappings(PredictionContext, rows)
        session.commit()
        return len(rows)
    except Exception:  # noqa: BLE001 — degrade to per-row so one bad row can't block all
        session.rollback()
        log.warning(
            "prediction_context: bulk insert of %d rows failed; retrying per-row",
            len(rows),
            exc_info=True,
        )
    written = 0
    for r in rows:
        try:
            session.add(PredictionContext(**r))
            session.commit()
            written += 1
        except Exception:  # noqa: BLE001 — skip the offending row, keep the rest
            session.rollback()
            log.warning(
                "prediction_context: skipped row for %s", r.get("prediction_id"), exc_info=True
            )
    log.info("prediction_context: wrote %d/%d rows via per-row fallback", written, len(rows))
    return written


def repair_null_context(session: Session) -> int:
    """Re-resolve context rows whose fields are all-null (sentinels + any legacy null
    rows) but whose evidence NOW resolves; update them in place. Self-heals a
    transiently-missed origin and recovers rows that were never stamped. Cheap: only
    null (source_class IS NULL) rows are considered, and there are few."""
    try:
        null_ids = [
            r[0]
            for r in session.execute(
                select(PredictionContext.prediction_id).where(
                    PredictionContext.source_class.is_(None)
                )
            ).all()
        ]
    except OperationalError:
        return 0
    if not null_ids:
        return 0

    ev_map: dict[str, Any] = {}
    for i in range(0, len(null_ids), _CHUNK):
        chunk = null_ids[i : i + _CHUNK]
        for pid, ev in session.execute(
            select(Prediction.prediction_id, Prediction.evidence_json).where(
                Prediction.prediction_id.in_(chunk)
            )
        ).all():
            ev_map[pid] = ev

    direct: dict[str, list[str]] = {}
    all_cids: set[str] = set()
    for pid in null_ids:
        cids = _origin_cluster_ids(_coerce_evidence(ev_map.get(pid)))
        if cids:
            direct[pid] = cids
            all_cids.update(cids)
    origin = _cluster_origin_map(session, all_cids)

    repaired = 0
    for pid, cids in direct.items():
        ctx = _ctx_from_map(cids, origin)
        if ctx is None:
            continue
        row = session.get(PredictionContext, pid)
        if row is not None:
            for k, v in ctx.items():
                setattr(row, k, v)
            repaired += 1
    if repaired:
        session.commit()
        log.info("prediction_context: repaired %d null-field context row(s) in place", repaired)
    return repaired


def backfill_prediction_context(session: Session, *, only_missing: bool = True) -> int:
    """Insert context rows for predictions missing one; commit. Returns rows written.

    Then, on the per-cycle path, repair any null-field rows whose evidence now
    resolves (self-heal). Resilient throughout: no single bad row or transient miss
    can silently block the rest."""
    written = _insert_rows(session, compute_context_rows(session, only_missing=only_missing))
    if only_missing:
        repair_null_context(session)
    return written


def resolve_debug(session: Session, prediction_id: str) -> dict[str, Any]:
    """Read-only diagnostic for ONE prediction: does a context row exist (and its raw
    fields)? what is evidence_json's stored value + Python type? would the anti-join
    treat it as missing? and what does the resolver return for it RIGHT NOW (dry run,
    any exception captured as text)? No writes. Powers /predictions/{id}/context-debug."""
    pred = session.get(Prediction, prediction_id)
    out: dict[str, Any] = {"prediction_id": prediction_id, "prediction_exists": pred is not None}
    if pred is None:
        return out

    ev_raw = pred.evidence_json
    out["issued_at"] = pred.issued_at.isoformat() if pred.issued_at else None
    out["config_version"] = pred.config_version
    out["evidence"] = {"python_type": type(ev_raw).__name__, "raw_repr": repr(ev_raw)[:2000]}

    ctx_row = session.get(PredictionContext, prediction_id)
    out["context_row_exists"] = ctx_row is not None
    out["context_row"] = (
        None
        if ctx_row is None
        else {
            "source_class": ctx_row.source_class,
            "headline": ctx_row.headline,
            "url": ctx_row.url,
            "source": ctx_row.source,
            "cluster_id": ctx_row.cluster_id,
            "created_at": ctx_row.created_at.isoformat() if ctx_row.created_at else None,
            "all_null_sentinel": ctx_row.source_class is None,
        }
    )
    # only_missing selects rows with NO context row at all.
    out["anti_join_selects_as_missing"] = ctx_row is None

    dbg: dict[str, Any] = {}
    try:
        ev = _coerce_evidence(ev_raw)
        dbg["coerced_evidence_type"] = type(ev).__name__
        dbg["coerced_was_string"] = isinstance(ev_raw, str)
        cids = _origin_cluster_ids(ev)
        dbg["extracted_cluster_ids"] = cids
        origin = _cluster_origin_map(session, set(cids))
        dbg["cluster_lookup"] = {
            c: (
                {"found": False}
                if c not in origin
                else {
                    "found": True,
                    "source_class": origin[c][0],
                    "url": origin[c][1],
                    "source": origin[c][2],
                    "title": origin[c][3],
                }
            )
            for c in cids
        }
        dbg["resolved_ctx"] = _ctx_from_map(cids, origin)
        dbg["exception"] = None
    except Exception as e:  # noqa: BLE001 — the whole point is to surface the failure text
        dbg["exception"] = f"{type(e).__name__}: {e}"
    out["dry_run_resolve"] = dbg
    return out
