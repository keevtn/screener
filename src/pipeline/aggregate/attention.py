"""Attention rollup + buzz baselines (the attention-baseline layer).

Two derived artifacts, both rebuilt idempotently:

- ``attention_daily``: per-ticker daily news volume (structured + social) and mean
  sentiment, aggregated from clusters + attributions. Summed across every source
  DB passed in (the live pipeline DB + the legacy import), so history accumulates.

- ``buzz_baselines``: per-ticker mean/std of daily *social* volume, winsorized
  against meme spikes and shrunk toward the global mean (empirical-Bayes warm
  start). buzz_z = (social_count - mean) / std. Tickers with too little social
  history get no baseline and therefore no buzz (price-only tracking) — the
  intended tiering.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date as date_
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pipeline.common.models import (
    AttentionDaily,
    BuzzBaseline,
    Cluster,
    ClusterEntity,
    ClusterScore,
    RawItem,
)
from pipeline.common.timeutil import utcnow

_STD_FLOOR = 1.0  # never divide buzz_z by a near-zero std


def _rollup_source(engine: Engine) -> dict[tuple[str, date_], dict[str, Any]]:
    """Aggregate (ticker, date) -> counts + sentiment sum/n from ONE source DB.

    Uses clusters (the deduped story unit, I5), attributed via cluster_entities,
    dated by the origin item's published_at, split by source_class.
    """
    agg: dict[tuple[str, date_], dict[str, Any]] = {}
    with Session(engine) as s:
        rows = s.execute(
            select(
                ClusterEntity.ticker,
                RawItem.published_at,
                RawItem.source_class,
                ClusterScore.finbert_score,
            )
            .join(Cluster, Cluster.cluster_id == ClusterEntity.cluster_id)
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
        ).all()
    for ticker, published_at, source_class, sent in rows:
        key = (ticker, published_at.date())
        a = agg.setdefault(key, {"struct": 0, "social": 0, "sent_sum": 0.0, "sent_n": 0})
        if source_class == "social":
            a["social"] += 1
        else:
            a["struct"] += 1
        if sent is not None:
            a["sent_sum"] += float(sent)
            a["sent_n"] += 1
    return agg


def build_attention_daily(
    session: Session, source_engines: list[Engine], *, now: datetime | None = None
) -> int:
    """Full idempotent recompute of attention_daily, summing across all sources."""
    now = now or utcnow()
    combined: dict[tuple[str, date_], dict[str, Any]] = {}
    for eng in source_engines:
        for key, a in _rollup_source(eng).items():
            c = combined.setdefault(key, {"struct": 0, "social": 0, "sent_sum": 0.0, "sent_n": 0})
            c["struct"] += a["struct"]
            c["social"] += a["social"]
            c["sent_sum"] += a["sent_sum"]
            c["sent_n"] += a["sent_n"]

    session.execute(delete(AttentionDaily))
    for (ticker, d), a in combined.items():
        session.add(
            AttentionDaily(
                ticker=ticker,
                date=d,
                struct_count=a["struct"],
                social_count=a["social"],
                sentiment_mean=(a["sent_sum"] / a["sent_n"] if a["sent_n"] else None),
                updated_at=now,
            )
        )
    session.commit()
    return len(combined)


def _winsorize(values: list[float], pct: float) -> list[float]:
    """Clip values above the pct-th percentile to that percentile (tame spikes)."""
    if not values:
        return values
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(pct * (len(ordered) - 1)))
    cap = ordered[idx]
    return [min(v, cap) for v in values]


def compute_buzz_baselines(
    session: Session,
    *,
    shrink_k: float = 10.0,
    winsor_pct: float = 0.95,
    min_days: int = 3,
    source: str = "warm_start",
    now: datetime | None = None,
) -> int:
    """Recompute per-ticker buzz baselines from attention_daily social volume.

    Winsorize each ticker's daily social counts, take mean/std, then shrink toward
    the global mean/std with strength ``shrink_k`` (low-n tickers are pulled hard
    toward the prior). Tickers with < ``min_days`` social-active days get no row.
    """
    now = now or utcnow()
    series: dict[str, list[float]] = defaultdict(list)
    for ticker, social in session.execute(
        select(AttentionDaily.ticker, AttentionDaily.social_count).where(
            AttentionDaily.social_count > 0
        )
    ).all():
        series[ticker].append(float(social))

    # Per-ticker winsorized mean/std for tickers clearing the floor.
    raw: dict[str, tuple[float, float, int]] = {}
    for ticker, counts in series.items():
        if len(counts) < min_days:
            continue
        w = _winsorize(counts, winsor_pct)
        m = statistics.fmean(w)
        sd = statistics.pstdev(w) if len(w) > 1 else 0.0
        raw[ticker] = (m, sd, len(counts))
    if not raw:
        session.execute(delete(BuzzBaseline))
        session.commit()
        return 0

    global_mean = statistics.fmean([m for m, _, _ in raw.values()])
    global_std = statistics.fmean([sd for _, sd, _ in raw.values()])

    session.execute(delete(BuzzBaseline))
    for ticker, (m, sd, n) in raw.items():
        # Empirical-Bayes shrink toward the global prior.
        sm = (n * m + shrink_k * global_mean) / (n + shrink_k)
        ss = (n * sd + shrink_k * global_std) / (n + shrink_k)
        session.add(
            BuzzBaseline(
                ticker=ticker,
                mean=round(sm, 3),
                std=round(max(ss, _STD_FLOOR), 3),
                n_days=n,
                source=source,
                updated_at=now,
            )
        )
    session.commit()
    return len(raw)


def buzz_z(social_count: int, baseline: BuzzBaseline | None) -> float | None:
    """Standardized buzz for a day, or None if the ticker has no baseline."""
    if baseline is None:
        return None
    return round((social_count - baseline.mean) / max(baseline.std, _STD_FLOOR), 2)
