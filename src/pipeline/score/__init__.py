"""Two-axis, cluster-scoped scoring (docs/ROADMAP.md Phase 3).

Sentiment axis (3.1, separate finbert/lm scores — I7), catalyst materiality axis
(3.2, rules over configs/catalysts.yaml — I11), text-kind routing (3.3), and the
earnings-surprise guard (3.4). Scoring runs once per cluster on the origin item's
text (I5); no LLM in the path (I6).
"""

from pipeline.score.catalysts import (
    CatalystResult,
    CatalystTaxonomy,
    classify_catalyst,
    load_taxonomy,
)
from pipeline.score.routing import text_kind_of
from pipeline.score.score import (
    ClusterScoreValues,
    persist_cluster_score,
    score_cluster,
    score_clusters,
)
from pipeline.score.sentiment import SentimentScores, score_sentiment

__all__ = [
    "CatalystResult",
    "CatalystTaxonomy",
    "ClusterScoreValues",
    "SentimentScores",
    "classify_catalyst",
    "load_taxonomy",
    "persist_cluster_score",
    "score_cluster",
    "score_clusters",
    "score_sentiment",
    "text_kind_of",
]
