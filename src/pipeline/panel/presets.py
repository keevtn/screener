"""Presets: named catalyst bundles that compile to one filter object (task 5b.3, I11).

A preset compiles to the SAME stored filter object used by the screener, alerts, and
the Phase 7 candidate filter. Adding a preset in configs/presets.yaml needs zero code
changes — the compiler is generic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config_files import configs_dir
from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, RawItem


def load_presets(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    p = Path(path) if path else configs_dir() / "presets.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("presets", {}) or {}


def compile_preset(preset: dict[str, Any]) -> dict[str, Any]:
    """Normalize a preset into the canonical filter object (deterministic).

    The same object drives the screener, alerts, and the Phase 7 candidate filter
    (I11). `min_abs_sentiment` (0 = off) lets a preset gate on sentiment magnitude
    — the ranker's extreme_sentiment term rides this shared field.
    """
    stages = preset.get("stages") or None
    return {
        "catalyst_types": sorted(preset.get("catalyst_types", [])),
        "stages": sorted(stages) if stages else None,
        "min_materiality": float(preset.get("min_materiality", 0.0)),
        "high_alert_only": bool(preset.get("high_alert_only", False)),
        "min_abs_sentiment": float(preset.get("min_abs_sentiment", 0.0)),
    }


def screen(
    session: Session, filter_obj: dict[str, Any], *, limit: int = 100
) -> list[dict[str, Any]]:
    """Apply a compiled filter to fired catalysts (the screener consumer)."""
    conds = []
    if filter_obj.get("catalyst_types"):
        conds.append(ClusterScore.catalyst_type.in_(filter_obj["catalyst_types"]))
    if filter_obj.get("stages"):
        conds.append(ClusterScore.event_stage.in_(filter_obj["stages"]))
    conds.append(ClusterScore.materiality >= filter_obj.get("min_materiality", 0.0))
    if filter_obj.get("high_alert_only"):
        conds.append(ClusterScore.high_alert.is_(True))
    if filter_obj.get("min_abs_sentiment"):
        cutoff = float(filter_obj["min_abs_sentiment"])
        conds.append(func.abs(func.coalesce(ClusterScore.finbert_score, 0.0)) >= cutoff)

    rows = session.execute(
        select(ClusterScore, Cluster, RawItem)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(*conds)
        .order_by(ClusterScore.materiality.desc())
        .limit(limit)
    ).all()
    out = []
    for cs, cluster, origin in rows:
        tickers = session.execute(
            select(ClusterEntity.ticker, ClusterEntity.ticker_role).where(
                ClusterEntity.cluster_id == cluster.cluster_id
            )
        ).all()
        out.append(
            {
                "cluster_id": cluster.cluster_id,
                "catalyst_type": cs.catalyst_type,
                "event_stage": cs.event_stage,
                "materiality": cs.materiality,
                "high_alert": cs.high_alert,
                "published_at": origin.published_at.isoformat(),
                "title": (origin.payload_json or {}).get("title"),
                "tickers": [{"ticker": t, "role": r} for t, r in tickers],
            }
        )
    return out
