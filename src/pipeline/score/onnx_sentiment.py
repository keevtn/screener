"""Quantized-FinBERT sentiment via ONNX Runtime — the Railway scoring backend.

This is the small-footprint alternative to the torch/transformers FinBERT in
backend/sentiment.py. It runs an int8-quantized ProsusAI/finbert exported to ONNX
(scripts/export_finbert_onnx.py, run LOCALLY) and needs ONLY onnxruntime +
tokenizers + numpy at runtime — no torch, no transformers, no optimum. That keeps
the Railway image lean and RSS comfortably under the trial cap: the int8 graph is
~110 MB on disk and the session holds it once, reused across every sweep.

Interface parity with the other analyzers: `analyze_text_batch(pairs) -> list`
of results carrying `.label` ("bullish"/"bearish"/"neutral"), `.score`
(P(pos)-P(neg) in [-1, 1]), and `.confidence` — exactly what score.py reads (I7:
finbert_score is stored separately, never pre-blended).

Artifacts (paths overridable by env):
  * model   — FINBERT_ONNX_PATH   (default data/models/finbert-int8.onnx),
              downloaded to the volume at boot by scripts/fetch_model.py.
  * tokenizer — FINBERT_TOKENIZER_DIR (default models/finbert), a committed
              tokenizer.json (small) produced by the export script.

Everything heavy is imported INSIDE __init__ so merely importing this module (or
falling back to lexicon-only) costs nothing. If the model or deps are missing the
constructor raises, and resolve_finbert() downgrades to lexicon-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ProsusAI/finbert config.json id2label: 0=positive, 1=negative, 2=neutral.
# Overridable in case a re-export reorders the head (comma-separated logit order).
_DEFAULT_LABEL_ORDER = "positive,negative,neutral"

# Directional thresholds — identical to backend._label_from_score so ONNX and the
# lexicon/torch paths label the same score the same way.
_POS_THRESHOLD = 0.05
_NEG_THRESHOLD = -0.05


@dataclass(frozen=True)
class _OnnxResult:
    score: float
    label: str
    confidence: float


def _label_from_score(score: float) -> str:
    if score >= _POS_THRESHOLD:
        return "bullish"
    if score <= _NEG_THRESHOLD:
        return "bearish"
    return "neutral"


class OnnxFinbertAnalyzer:
    """int8 ONNX FinBERT. Constructing it loads the session + tokenizer eagerly so a
    missing/corrupt artifact fails fast (→ lexicon fallback) rather than mid-sweep."""

    def __init__(
        self,
        model_path: str | None = None,
        tokenizer_dir: str | None = None,
        *,
        max_length: int = 512,
        num_threads: int | None = None,
    ) -> None:
        import numpy as np  # noqa: PLC0415 — heavy deps kept out of module import
        import onnxruntime as ort  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        self._np = np
        model_path = model_path or os.environ.get(
            "FINBERT_ONNX_PATH", "data/models/finbert-int8.onnx"
        )
        tokenizer_dir = tokenizer_dir or os.environ.get("FINBERT_TOKENIZER_DIR", "models/finbert")
        tok_json = Path(tokenizer_dir) / "tokenizer.json"
        if not Path(model_path).exists():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")
        if not tok_json.exists():
            raise FileNotFoundError(f"tokenizer.json not found: {tok_json}")

        self._tok = Tokenizer.from_file(str(tok_json))
        self._tok.enable_truncation(max_length)  # BERT hard cap — TOKEN-level (see backend note)
        self._tok.enable_padding()  # pad to the longest sequence in each batch

        so = ort.SessionOptions()
        # Small instance: cap threads so scoring never steals the box from the API.
        threads = num_threads if num_threads is not None else int(
            os.environ.get("FINBERT_ONNX_THREADS", "1")
        )
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(
            model_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._sess.get_inputs()}

        order = os.environ.get("FINBERT_LABEL_ORDER", _DEFAULT_LABEL_ORDER).split(",")
        idx = {name.strip().lower(): i for i, name in enumerate(order)}
        self._i_pos = idx["positive"]
        self._i_neg = idx["negative"]
        self._i_neu = idx.get("neutral", 3 - self._i_pos - self._i_neg)

    def analyze_text_batch(self, pairs: list[tuple[str, str]]) -> list[_OnnxResult]:
        if not pairs:
            return []
        np = self._np
        texts = [f"{title}. {description}".strip() for title, description in pairs]
        encs = self._tok.encode_batch(texts)
        feed = {
            "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
        }
        feed = {k: v for k, v in feed.items() if k in self._input_names}
        logits = self._sess.run(None, feed)[0]
        # Numerically stable softmax over the 3-class head.
        shifted = logits - logits.max(axis=1, keepdims=True)
        exp = np.exp(shifted)
        probs = exp / exp.sum(axis=1, keepdims=True)

        out: list[_OnnxResult] = []
        for row in probs:
            pos = float(row[self._i_pos])
            neg = float(row[self._i_neg])
            neu = float(row[self._i_neu])
            score = pos - neg
            out.append(
                _OnnxResult(
                    score=round(score, 4),
                    label=_label_from_score(score),
                    confidence=round(max(pos, neg, neu), 4),
                )
            )
        return out
