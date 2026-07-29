"""Signal lab — event-study evaluation of raw scores (docs/ROADMAP.md Phase 5c).

Grades INPUTS (do raw scores carry information?) on every ticker-attributed scored
cluster, not just threshold-crossers. Observations are written at scoring time
(5c.1), marked against forward abnormal returns with no look-ahead (5c.2, I12),
confounding-controlled (5c.3), and analyzed for IC / CAR (5c.4) behind a frozen
holdout (5c.5). Backfilled/imported observations are labelled and excluded from
headline stats by default.
"""

from pipeline.lab.analysis import car_curves, load_lab_rows, quintile_spread, spearman_ic
from pipeline.lab.marking import mark_observation, mark_observations
from pipeline.lab.observe import observation_id, observe_scored_clusters

__all__ = [
    "car_curves",
    "load_lab_rows",
    "mark_observation",
    "mark_observations",
    "observation_id",
    "observe_scored_clusters",
    "quintile_spread",
    "spearman_ic",
]
