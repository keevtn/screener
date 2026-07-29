"""Nightly grading job (docs/ROADMAP.md task 5.1).

Grades every ``open`` prediction whose horizon has fully elapsed; predictions
still inside their horizon (or lacking bars) stay open. Idempotent: it only
touches ``status='open'`` rows and the grader fills the I4 outcome fields only, so
re-running never regrades or double-writes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import Prediction
from pipeline.grade.grader import (
    DEFAULT_CLOSE_TIME,
    DEFAULT_EXCHANGE_TZ,
    apply_grade,
    grade_prediction,
)


def grade_open_predictions(
    session: Session,
    provider: Any,
    *,
    exchange_tz: str = DEFAULT_EXCHANGE_TZ,
    close_time: str = DEFAULT_CLOSE_TIME,
) -> tuple[int, int]:
    """Grade all resolvable open predictions. Returns (graded, left_open)."""
    open_preds = (
        session.execute(select(Prediction).where(Prediction.status == "open")).scalars().all()
    )
    graded = skipped = 0
    for pred in open_preds:
        result = grade_prediction(pred, provider, exchange_tz=exchange_tz, close_time=close_time)
        if result is None:
            skipped += 1
            continue
        apply_grade(session, pred, result)
        graded += 1
    return graded, skipped
