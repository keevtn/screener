"""Grader: resolve open predictions against market-adjusted price outcomes.

docs/ROADMAP.md task 0.5, implementing docs/prediction-contract-v1.md exactly.
"""

from pipeline.grade.baselines import emit_baselines, ensure_baseline_configs
from pipeline.grade.grader import Grader, GradeResult, apply_grade, grade_prediction
from pipeline.grade.job import grade_open_predictions
from pipeline.grade.metrics import Metrics, compute_metrics, metrics_by_config

__all__ = [
    "GradeResult",
    "Grader",
    "Metrics",
    "apply_grade",
    "compute_metrics",
    "emit_baselines",
    "ensure_baseline_configs",
    "grade_open_predictions",
    "grade_prediction",
    "metrics_by_config",
]
