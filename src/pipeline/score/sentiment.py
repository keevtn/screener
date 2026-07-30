"""Cluster-scoped sentiment (docs/ROADMAP.md task 3.1, invariant I7).

FinBERT and Loughran–McDonald scores are computed and stored SEPARATELY
(finbert_label/finbert_score, lm_score) — never pre-blended. Blend weights live in
config and apply only at aggregation (Phase 4). Analyzers are injectable so tests
run on the zero-dependency L-M lexicon + a fake FinBERT (no torch in CI).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_BACKEND = Path(__file__).resolve().parents[3] / "backend"

log = logging.getLogger("pipeline.score.sentiment")


def finbert_status_path() -> Path:
    """Where resolve_finbert records its outcome so the read-only API can surface
    it (the pipeline scores; the API doesn't — this bridges them without logs)."""
    return Path(os.environ.get("FINBERT_STATUS_PATH", "data/finbert_status.json"))


def _write_finbert_status(mode: str, active: bool, error: str | None, score: float | None) -> None:
    """Persist the FinBERT resolve outcome (best-effort; never raises)."""
    try:
        p = finbert_status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"mode": mode, "active": active, "error": error, "probe_score": score}
            )
        )
    except Exception:  # noqa: BLE001 — observability only, never break scoring
        pass


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
        _write_finbert_status(mode, active=False, error=None, score=None)
        return None
    if mode in ("onnx", "onnx-int8", "quantized"):
        try:
            from pipeline.score.onnx_sentiment import OnnxFinbertAnalyzer

            analyzer = OnnxFinbertAnalyzer()
            # SELF-TEST: run one real inference at construction. Loading the ONNX
            # session can succeed while inference fails at RUNTIME on a given
            # box (Linux onnxruntime op/threading/memory issues the Windows dev
            # box never hits) — and an inference that throws mid-sweep crashes the
            # ENTIRE score step, producing ZERO cluster_scores (observed on Railway
            # 2026-07-30: onnx mode -> clusters grew but cluster_scores stayed 0,
            # while lexicon mode scored fine). Catching it here degrades to LM so
            # scoring keeps writing cluster_scores, and logs the exact error so the
            # real onnx failure is visible instead of a silently-dead pipeline.
            probe = analyzer.analyze_text_batch([("probe", "the market rallied on strong earnings")])
            if not probe or probe[0].score is None:
                raise RuntimeError("ONNX self-test produced no score")
            log.info("sentiment: ONNX int8 FinBERT active (self-test ok, score=%.4f)", probe[0].score)
            _write_finbert_status(mode, active=True, error=None, score=float(probe[0].score))
            return analyzer
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the pipeline
            log.warning(
                "SENTIMENT_MODE=%s but ONNX FinBERT unavailable at construct OR self-test "
                "inference (%r); scoring LM-only (cluster_scores still written, finbert_score null)",
                mode, exc,
            )
            _write_finbert_status(mode, active=False, error=f"{type(exc).__name__}: {exc}"[:400], score=None)
            return None
    if mode in ("finbert", "torch", "transformers"):
        try:
            return default_finbert()
        except Exception as exc:  # noqa: BLE001
            log.warning("SENTIMENT_MODE=%s but torch FinBERT unavailable (%s); LM-only", mode, exc)
            return None
    log.warning("unknown SENTIMENT_MODE=%r; scoring LM-only", mode)
    return None
