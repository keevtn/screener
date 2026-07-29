"""Metrics over the graded ledger (docs/ROADMAP.md task 5.3).

Hit rate, per-class precision/recall, coverage, and mean lead time — grouped by
config_version (so skill and baselines compare uniformly). Per-class precision/
recall treat the realized direction as ground truth: a resolved prediction's actual
direction is its predicted direction if ``correct`` else the opposite.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import Prediction


@dataclass
class Metrics:
    config_version: str
    total_graded: int = 0
    correct: int = 0
    incorrect: int = 0
    expired: int = 0
    hit_rate: float | None = None  # correct / resolved
    coverage: float | None = None  # resolved / graded (crossing rate)
    precision: dict[str, float | None] = field(default_factory=dict)
    recall: dict[str, float | None] = field(default_factory=dict)
    mean_lead_time_days: float | None = None

    @property
    def resolved(self) -> int:
        return self.correct + self.incorrect


def _safe_div(n: int, d: int) -> float | None:
    return n / d if d else None


def _lead_time_days(issued_date, resolving_close) -> int | None:
    if resolving_close is None:
        return None
    # Trading-day-ish gap: business days strictly after issue up to the close.
    return int(len(pd.bdate_range(issued_date, resolving_close))) - 1


def compute_metrics(preds: Iterable[Prediction], config_version: str) -> Metrics:
    m = Metrics(config_version=config_version)
    # 2x2 by (predicted direction, correctness) for precision/recall.
    tp = {"bullish": 0, "bearish": 0}  # predicted X, correct
    fp = {"bullish": 0, "bearish": 0}  # predicted X, incorrect
    leads: list[int] = []

    for p in preds:
        if p.status != "graded":
            continue
        m.total_graded += 1
        if p.outcome == "correct":
            m.correct += 1
            tp[p.direction] += 1
            lead = _lead_time_days(p.issued_at.date(), p.resolving_close)
            if lead is not None:
                leads.append(lead)
        elif p.outcome == "incorrect":
            m.incorrect += 1
            fp[p.direction] += 1
        elif p.outcome == "expired":
            m.expired += 1

    m.hit_rate = _safe_div(m.correct, m.resolved)
    m.coverage = _safe_div(m.resolved, m.total_graded)
    # Actual-direction ground truth: incorrect X means the move went the OTHER way.
    for cls, other in (("bullish", "bearish"), ("bearish", "bullish")):
        m.precision[cls] = _safe_div(tp[cls], tp[cls] + fp[cls])
        m.recall[cls] = _safe_div(tp[cls], tp[cls] + fp[other])
    m.mean_lead_time_days = (sum(leads) / len(leads)) if leads else None
    return m


def metrics_by_config(session: Session) -> dict[str, Metrics]:
    """One Metrics per config_version present in the graded ledger."""
    graded = (
        session.execute(select(Prediction).where(Prediction.status == "graded")).scalars().all()
    )
    by_cv: dict[str, list[Prediction]] = {}
    for p in graded:
        by_cv.setdefault(p.config_version, []).append(p)
    return {cv: compute_metrics(preds, cv) for cv, preds in by_cv.items()}
