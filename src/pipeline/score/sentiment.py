"""Cluster-scoped sentiment (docs/ROADMAP.md task 3.1, invariant I7).

FinBERT and Loughran–McDonald scores are computed and stored SEPARATELY
(finbert_label/finbert_score, lm_score) — never pre-blended. Blend weights live in
config and apply only at aggregation (Phase 4). Analyzers are injectable so tests
run on the zero-dependency L-M lexicon + a fake FinBERT (no torch in CI).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_BACKEND = Path(__file__).resolve().parents[3] / "backend"

log = logging.getLogger("pipeline.score.sentiment")


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


def resolve_finbert() -> _Analyzer | None:
    """Pick the FinBERT backend from $SENTIMENT_MODE, with automatic lexicon fallback.

    Modes (default ``lexicon``):
      * ``lexicon`` / ``lm`` / ``none`` → None (LM-only; the safe Railway default).
      * ``onnx``    → the int8 ONNX analyzer (onnxruntime + tokenizers; small image).
      * ``finbert`` / ``torch`` → the torch/transformers FinBERT (local/dev, ~440MB).

    Any failure to construct the requested analyzer (missing model, missing deps,
    corrupt artifact) is logged and downgraded to None — scoring then runs LM-only
    rather than crashing the sweep. Callers still store finbert_score separately (I7).
    """
    mode = os.environ.get("SENTIMENT_MODE", "lexicon").strip().lower()
    if mode in ("", "lexicon", "lm", "none", "off"):
        return None
    if mode in ("onnx", "onnx-int8", "quantized"):
        try:
            from pipeline.score.onnx_sentiment import OnnxFinbertAnalyzer

            analyzer = OnnxFinbertAnalyzer()
            log.info("sentiment: ONNX int8 FinBERT active")
            return analyzer
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the pipeline
            log.warning(
                "SENTIMENT_MODE=%s but ONNX FinBERT unavailable (%s); scoring LM-only", mode, exc
            )
            return None
    if mode in ("finbert", "torch", "transformers"):
        try:
            return default_finbert()
        except Exception as exc:  # noqa: BLE001
            log.warning("SENTIMENT_MODE=%s but torch FinBERT unavailable (%s); LM-only", mode, exc)
            return None
    log.warning("unknown SENTIMENT_MODE=%r; scoring LM-only", mode)
    return None
