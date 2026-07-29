"""Candidate filter for the ranker (docs/ROADMAP.md task 7.1, invariant I11).

The candidate set is a COMPOSITION (union) of the same compiled filter objects the
Phase 5b presets produce, plus a sentiment-magnitude term. It is not a parallel
mechanism: each member is the canonical `{catalyst_types, stages, min_materiality,
high_alert_only, min_abs_sentiment}` object, so adding a preset in presets.yaml
extends the ranker's reach with zero code changes.

Default: high_alert union extreme_sentiment. The roadmap's third default term,
buzz_spike, activates in Phase 6 (social buzz baselines) — until then it is a
no-op and is flagged in the morning notes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, RawItem
from pipeline.common.timeutil import utcnow
from pipeline.panel.presets import compile_preset, load_presets

# The default composite: high-alert clusters or strong-sentiment clusters.
DEFAULT_SENTIMENT_CUTOFF = 0.50
DEFAULT_RECENCY_DAYS = 7

# How many candidate tickers to feed the ranker per run. The ranker batches all
# candidates into ONE cached-system call, so breadth costs input tokens linearly:
# each candidate's evidence bundle is ~450 input tokens (≤6 clusters), so 50
# candidates ≈ 22K in + ~3K out ≈ $0.11/run at Sonnet-5 pricing ($3/$15 per MTok)
# — ~18 runs/day under the $2 soft cap. 25 was too shallow to be a real watchlist;
# 50 doubles reach while staying comfortably in budget. Override with
# AGENT_RANKER_CANDIDATES (the force-run `limit` param overrides per run).
DEFAULT_RANKER_CANDIDATES = 50


def default_ranker_candidates() -> int:
    """Default candidate breadth per ranker run (env AGENT_RANKER_CANDIDATES)."""
    raw = os.environ.get("AGENT_RANKER_CANDIDATES", str(DEFAULT_RANKER_CANDIDATES))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RANKER_CANDIDATES


def build_candidate_filter(
    *,
    presets: list[str] | None = None,
    high_alert: bool = True,
    extreme_sentiment: bool = True,
    sentiment_cutoff: float = DEFAULT_SENTIMENT_CUTOFF,
    recency_days: int = DEFAULT_RECENCY_DAYS,
    preset_defs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compose the union filter spec from named presets + the default terms (I11)."""
    defs = preset_defs if preset_defs is not None else load_presets()
    members: list[dict[str, Any]] = []
    for name in presets or []:
        if name not in defs:
            raise ValueError(f"unknown preset: {name}")
        members.append(compile_preset(defs[name]))
    if high_alert:
        members.append(compile_preset({"high_alert_only": True}))
    if extreme_sentiment:
        members.append(compile_preset({"min_abs_sentiment": sentiment_cutoff}))
    return {"union": members, "recency_days": recency_days}


def _member_conditions(member: dict[str, Any]) -> list[Any]:
    """SQL conditions for one canonical filter object (same shape as presets)."""
    conds: list[Any] = []
    if member.get("catalyst_types"):
        conds.append(ClusterScore.catalyst_type.in_(member["catalyst_types"]))
    if member.get("stages"):
        conds.append(ClusterScore.event_stage.in_(member["stages"]))
    if member.get("min_materiality"):
        conds.append(ClusterScore.materiality >= member["min_materiality"])
    if member.get("high_alert_only"):
        conds.append(ClusterScore.high_alert.is_(True))
    if member.get("min_abs_sentiment"):
        cutoff = float(member["min_abs_sentiment"])
        conds.append(
            or_(
                func.abs(ClusterScore.finbert_score) >= cutoff,
                func.abs(ClusterScore.lm_score) >= cutoff,
            )
        )
    return conds


def select_candidates(
    session: Session,
    filter_spec: dict[str, Any],
    *,
    limit: int = 25,
    now: datetime | None = None,
) -> list[str]:
    """Distinct recent tickers matching ANY union member, best-materiality first."""
    now = now or utcnow()
    # float, not int: the PMR overlay passes a fractional overnight window
    # (~0.7 days) — int() would truncate it to a zero-width cutoff.
    cutoff_ts = now - timedelta(days=float(filter_spec.get("recency_days", DEFAULT_RECENCY_DAYS)))
    members = filter_spec.get("union", [])
    if not members:
        return []
    # OR the members together; a ticker qualifies if any member matches one of its
    # recent scored clusters. Rank tickers by their strongest recent materiality.
    member_clauses = [
        and_(*conds) if conds else true() for conds in map(_member_conditions, members)
    ]
    member_or = or_(*member_clauses)
    stmt = (
        select(ClusterEntity.ticker, func.max(ClusterScore.materiality))
        .join(Cluster, Cluster.cluster_id == ClusterEntity.cluster_id)
        .join(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(RawItem.published_at >= cutoff_ts)
        .where(member_or)
        .group_by(ClusterEntity.ticker)
        .order_by(func.max(ClusterScore.materiality).desc())
        .limit(limit)
    )
    return [row[0] for row in session.execute(stmt).all()]
