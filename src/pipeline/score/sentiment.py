"""Cluster-scoped sentiment (docs/ROADMAP.md task 3.1, invariant I7).

FinBERT and Loughran–McDonald scores are computed and stored SEPARATELY
(finbert_label/finbert_score, lm_score) — never pre-blended. Blend weights live in
config and apply only at aggregation (Phase 4). Analyzers are injectable so tests
run on the zero-dependency L-M lexicon + a fake FinBERT (no torch in CI).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_BACKEND = Path(__file__).resolve().parents[3] / "backend"


class _Analyzer(Protocol):
    def analyze_text_batch(self, pairs: list[tuple[str, str]]) -> list[Any]: ...


@dataclass(frozen=True)
class SentimentScores:
    finbert_label: str | None = None
    finbert_score: float | None = None
    lm_score: float | None = None


def score_sentiment(
    title: str,
    description: str,
    *,
    finbert: _Analyzer | None = None,
    lm: _Analyzer | None = None,
) -> SentimentScores:
    """Score one cluster's origin text with whichever analyzers are supplied."""
    fb_label = fb_score = lm_score = None
    if lm is not None:
        lm_score = lm.analyze_text_batch([(title, description)])[0].score
    if finbert is not None:
        r = finbert.analyze_text_batch([(title, description)])[0]
        fb_label, fb_score = r.label, r.score
    return SentimentScores(fb_label, fb_score, lm_score)


def _import_backend_sentiment() -> Any:
    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    import sentiment  # noqa: PLC0415 — backend flat module, imported lazily

    return sentiment


def default_lm() -> _Analyzer:
    """The zero-dependency Loughran–McDonald lexicon analyzer (backend)."""
    return _import_backend_sentiment().LoughranMcDonaldAnalyzer()


def default_finbert() -> _Analyzer:
    """FinBERT (backend; needs torch/transformers, ~440MB). Local/dev only."""
    return _import_backend_sentiment().FinBERTAnalyzer()
