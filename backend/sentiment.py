"""
sentiment.py
============
Financial sentiment analysis: bearish / bullish / neutral classification.

Three interchangeable analyzers all implement the ``SentimentAnalyzer``
Protocol so the storage layer (``RedisHandler``) never needs to know which
model is active.  Pick one based on your accuracy / latency requirements:

  Analyzer                    Accuracy   Latency     Extra deps
  ──────────────────────────────────────────────────────────────
  FinBERTAnalyzer             High       ~0.5–2 s    transformers, torch
  LoughranMcDonaldAnalyzer    Medium     ~1 ms       none (built-in lexicon)
  VaderSentimentAnalyzer      Low        ~1 ms       vaderSentiment

All analyzers are synchronous.  ``RedisHandler`` calls them via
``asyncio.to_thread`` so FinBERT inference never blocks the event loop.

Dependencies (install only what you use):
    pip install transformers torch   # FinBERT
    pip install vaderSentiment       # VADER fallback
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from IngestionModule import NewsItem

log = logging.getLogger("ingestion_agent.sentiment")


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SentimentResult:
    """Output of every SentimentAnalyzer."""

    score: float       # continuous [-1.0, 1.0]; negative = bearish, positive = bullish
    label: str         # "bullish" | "bearish" | "neutral"
    confidence: float  # [0.0, 1.0]; how certain the model is in its label


# ---------------------------------------------------------------------------
# Protocol — the interface every analyzer must satisfy
# ---------------------------------------------------------------------------

class SentimentAnalyzer(Protocol):
    """Maps a NewsItem to a SentimentResult."""

    def analyze(self, item: NewsItem) -> SentimentResult: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _label_from_score(
    score: float,
    pos_threshold: float = 0.05,
    neg_threshold: float = -0.05,
) -> str:
    """Map a continuous score to a directional label."""
    if score >= pos_threshold:
        return "bullish"
    if score <= neg_threshold:
        return "bearish"
    return "neutral"


def _item_text(item: NewsItem, max_chars: int = 512) -> str:
    """Combine title + description into a single string for analysis."""
    return f"{item.title}. {item.description}".strip()[:max_chars]


# ---------------------------------------------------------------------------
# FinBERT — highest accuracy, finance-tuned transformer
# ---------------------------------------------------------------------------

class FinBERTAnalyzer:
    """
    Classifies financial text using ProsusAI/finbert — a BERT model fine-tuned
    on ~10 k financial news sentences annotated as positive / negative / neutral.

    Score is computed as P(positive) − P(negative) ∈ [-1, 1] so mixed signals
    produce a score near 0 rather than false high confidence.

    Parameters
    ----------
    model_name:
        HuggingFace model ID.  Defaults to "ProsusAI/finbert".
    device:
        -1 for CPU; 0+ for a CUDA GPU index.
    batch_size:
        Passed to the transformers pipeline for ``analyze_batch``.

    Installation
    ------------
        pip install transformers torch
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        device: int = -1,
        batch_size: int = 8,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._pipeline: Any = None   # lazy — loaded on first call

    def _load(self) -> None:
        try:
            from transformers import pipeline as hf_pipeline
        except ImportError:
            raise RuntimeError(
                "transformers is not installed — run: pip install transformers torch"
            )
        self._pipeline = hf_pipeline(
            "text-classification",
            model=self._model_name,
            device=self._device,
            top_k=None,   # return scores for all three classes, not just the top one
        )
        log.info(
            "FinBERTAnalyzer: loaded '%s' on device=%s",
            self._model_name, self._device,
        )

    def _result_from_raw(self, raw: list[dict[str, Any]]) -> SentimentResult:
        """Convert a single pipeline output (list of class dicts) to SentimentResult."""
        probs = {r["label"].lower(): r["score"] for r in raw}
        pos = probs.get("positive", 0.0)
        neg = probs.get("negative", 0.0)
        score = float(pos - neg)
        label = _label_from_score(score)
        confidence = float(max(pos, neg, probs.get("neutral", 0.0)))
        return SentimentResult(
            score=round(score, 4),
            label=label,
            confidence=round(confidence, 4),
        )

    # BERT hard cap. Truncation must happen at the TOKEN level: a [:512] char
    # slice is not enough — punctuation/digit-dense text tokenizes to >1 token
    # per char, and one such doc ("tensor a (528) vs b (512)") aborts the whole
    # batched score step, starving everything downstream (observed 2026-07-20).
    _TRUNC = {"truncation": True, "max_length": 512}

    def analyze(self, item: NewsItem) -> SentimentResult:
        if self._pipeline is None:
            self._load()
        raw = self._pipeline(_item_text(item), **self._TRUNC)
        # Newer transformers (>=4.35) wraps single-input top_k=None in a list-of-lists:
        # [[{label, score}, ...]] — unwrap to [{label, score}, ...] before scoring.
        if raw and isinstance(raw[0], list):
            raw = raw[0]
        return self._result_from_raw(raw)

    def analyze_batch(self, items: list[NewsItem]) -> list[SentimentResult]:
        """Process multiple NewsItems in one forward pass for higher throughput."""
        if self._pipeline is None:
            self._load()
        texts = [_item_text(i) for i in items]
        return [
            self._result_from_raw(raw)
            for raw in self._pipeline(texts, batch_size=self._batch_size, **self._TRUNC)
        ]

    def analyze_text_batch(
        self, pairs: list[tuple[str, str]]
    ) -> list[SentimentResult]:
        """
        Score a batch of raw (title, description) string pairs.

        Used by the middleware REST endpoint where NewsItem objects are not
        available — the caller passes plain strings from the JSON request body.
        """
        if self._pipeline is None:
            self._load()
        texts = [
            f"{title}. {description}".strip()[:512]
            for title, description in pairs
        ]
        return [
            self._result_from_raw(raw)
            for raw in self._pipeline(texts, batch_size=self._batch_size, **self._TRUNC)
        ]


# ---------------------------------------------------------------------------
# Loughran-McDonald lexicon — lightweight, no ML model required
# ---------------------------------------------------------------------------

# Weighted financial lexicon (Loughran-McDonald core + finance phrases + social
# slang). Three deliberate upgrades over a flat word list:
#
#   1. **Signed weights** — "bankruptcy" (−1.0) is not the same signal as
#      "concerns" (−0.3). A flat set treats them identically, which is exactly
#      how clear bearish stories end up scored neutral.
#   2. **Phrases before unigrams** — direction in finance lives in bigrams:
#      "beat estimates" vs "missed estimates", "raised guidance" vs "guidance
#      cut". Matched longest-first; matched spans are consumed so a phrase is
#      never double-counted by its member words.
#   3. **Negation flips** — "did not beat estimates" flips the phrase's sign
#      within a 3-token window (dampened, since negated praise is weaker than
#      direct criticism).
#
# Precision fix: the old bullish set contained non-directional nouns
# ("earnings", "revenue", "margin", "record", "income") that made every
# earnings-calendar headline read bullish. Direction is now carried by phrases
# ("record revenue", "revenue fell") — the bare nouns score nothing.
#
# For full LM coverage (~3 500 words), download the master dictionary CSV from
#     https://sraf.nd.edu/loughranmcdonald-master-dictionary/
# and pass its path as ``csv_path`` (words merge in at ±0.5 unless already
# present with a hand-tuned weight).

_UNIGRAMS: dict[str, float] = {
    # --- bearish: financial distress (strong) ---
    "bankrupt": -1.0, "bankruptcy": -1.0, "insolvent": -1.0, "insolvency": -1.0,
    "default": -0.9, "defaulted": -0.9, "delinquent": -0.7,
    "impairment": -0.8, "impaired": -0.8, "writedown": -0.8, "writeoff": -0.8,
    "restatement": -0.9, "restated": -0.7, "fraud": -1.0,
    "delisting": -0.9, "delisted": -0.9, "delist": -0.9,
    # --- bearish: misses / declines ---
    "missed": -0.7, "misses": -0.7, "shortfall": -0.8,
    "disappointing": -0.8, "disappointed": -0.7, "disappoints": -0.8,
    "underperformed": -0.7, "underperforms": -0.7, "underperform": -0.6,
    "declined": -0.6, "declining": -0.6, "decline": -0.6, "declines": -0.6,
    "loss": -0.6, "losses": -0.6, "deficit": -0.6, "deficits": -0.6,
    "reduction": -0.4, "reduced": -0.4, "decrease": -0.4, "decreased": -0.4,
    "drop": -0.6, "dropped": -0.6, "drops": -0.6,
    # --- bearish: price action (was entirely missing) ---
    "plunge": -0.8, "plunged": -0.8, "plunges": -0.8,
    "plummet": -0.8, "plummeted": -0.8, "plummets": -0.8,
    "tumble": -0.7, "tumbled": -0.7, "tumbles": -0.7,
    "sink": -0.7, "sinks": -0.7, "sank": -0.7,
    "slump": -0.7, "slumped": -0.7, "slumps": -0.7,
    "crash": -0.8, "crashed": -0.8, "crashes": -0.8,
    "collapse": -0.8, "collapsed": -0.8, "collapses": -0.8,
    "dive": -0.7, "dives": -0.7, "slide": -0.6, "slides": -0.6, "slid": -0.6,
    "fell": -0.6, "falls": -0.6, "falling": -0.5, "selloff": -0.7, "tanked": -0.7, "tanking": -0.7,
    # --- bearish: guidance / outlook ---
    "downgrade": -0.8, "downgraded": -0.8, "downgrades": -0.8,
    "lowered": -0.6, "lowers": -0.6, "warn": -0.6, "warning": -0.6, "warns": -0.6,
    "cautious": -0.4, "headwinds": -0.5, "challenges": -0.3, "difficult": -0.3,
    "uncertainty": -0.4, "uncertain": -0.4, "concern": -0.3, "concerns": -0.3,
    "risk": -0.2, "risks": -0.2, "overvalued": -0.6, "bearish": -0.9,
    # --- bearish: legal / regulatory ---
    "lawsuit": -0.6, "lawsuits": -0.6, "litigation": -0.6,
    "investigated": -0.7, "investigation": -0.7, "probe": -0.7,
    "subpoena": -0.8, "penalty": -0.6, "penalties": -0.6,
    "fine": -0.5, "fines": -0.5, "fined": -0.6,
    "violation": -0.7, "violations": -0.7, "misconduct": -0.8,
    "noncompliance": -0.7, "alleged": -0.5, "allegation": -0.5, "allegations": -0.5,
    "failure": -0.6, "deteriorated": -0.7,
    "rejected": -0.8, "rejection": -0.8, "rejects": -0.8, "denied": -0.6, "denial": -0.6,
    # --- bearish: operations / capital structure ---
    "layoffs": -0.6, "layoff": -0.6, "restructuring": -0.5, "restructure": -0.5,
    "recall": -0.8, "recalls": -0.8, "recalled": -0.8,
    "suspended": -0.7, "suspension": -0.7, "terminated": -0.6, "termination": -0.6,
    "delayed": -0.5, "delay": -0.5, "delays": -0.5,
    "disruption": -0.5, "disrupted": -0.5, "halted": -0.6,
    "dilution": -0.8, "dilutive": -0.7,
    # --- bearish: macro / social ---
    "recession": -0.6, "contraction": -0.5, "downturn": -0.6, "adverse": -0.5,
    "weak": -0.6, "weakness": -0.6, "volatile": -0.3, "volatility": -0.3,
    "deteriorating": -0.7, "deterioration": -0.7,
    "bagholder": -0.5, "bagholders": -0.5, "puts": -0.3,
    # --- bullish: beats / growth ---
    "beat": 0.6, "beats": 0.6, "exceeded": 0.8, "exceeds": 0.8,
    "surpassed": 0.8, "surpasses": 0.8, "topped": 0.7, "tops": 0.6,
    "outperformed": 0.7, "outperforms": 0.7, "outperform": 0.6,
    "growth": 0.5, "growing": 0.4, "grew": 0.5,
    "expand": 0.3, "expanded": 0.4, "expansion": 0.4,
    "accelerated": 0.5, "accelerate": 0.4, "accelerating": 0.5,
    "increase": 0.3, "increased": 0.3, "increases": 0.3,
    # --- bullish: price action (was entirely missing) ---
    "surge": 0.8, "surged": 0.8, "surges": 0.8,
    "soar": 0.8, "soared": 0.8, "soars": 0.8,
    "rally": 0.7, "rallied": 0.7, "rallies": 0.7,
    "jump": 0.7, "jumped": 0.7, "jumps": 0.7,
    "spike": 0.6, "spiked": 0.6, "spikes": 0.6,
    "climb": 0.5, "climbed": 0.5, "climbs": 0.5,
    "skyrocket": 0.9, "skyrocketed": 0.9, "skyrockets": 0.9,
    "rebound": 0.6, "rebounded": 0.6, "rebounds": 0.6,
    "rocketed": 0.8, "gains": 0.4, "gained": 0.4, "rip": 0.4, "ripping": 0.5,
    # --- bullish: profitability / strength ---
    "profit": 0.4, "profitable": 0.5, "profitability": 0.4,
    "strong": 0.5, "stronger": 0.5, "strength": 0.5, "robust": 0.5,
    "solid": 0.4, "healthy": 0.4, "momentum": 0.4, "leading": 0.3, "dominant": 0.4,
    # --- bullish: guidance / outlook / analyst ---
    "raised": 0.5, "raises": 0.5, "upgrade": 0.8, "upgraded": 0.8, "upgrades": 0.8,
    "favorable": 0.5, "improved": 0.5, "improving": 0.5, "improvement": 0.5,
    "recovery": 0.5, "recovered": 0.5, "overweight": 0.6, "undervalued": 0.5,
    "bullish": 0.9, "optimistic": 0.5, "confident": 0.4, "confidence": 0.3,
    # --- bullish: corporate actions / regulatory ---
    "acquisition": 0.3, "buyback": 0.7, "dividend": 0.3, "dividends": 0.3,
    "partnership": 0.5, "launch": 0.3, "launched": 0.3,
    "innovation": 0.3, "innovative": 0.3,
    "approval": 0.8, "approved": 0.8, "approves": 0.8,
    "clearance": 0.7, "cleared": 0.6, "uplisting": 0.6, "uplist": 0.6,
    # --- bullish: conviction / social ---
    "opportunity": 0.3, "opportunities": 0.3, "capitalize": 0.3, "advantage": 0.3,
    "upside": 0.5, "breakthrough": 0.7, "milestone": 0.5,
    "delivered": 0.3, "delivers": 0.3, "superior": 0.5,
    "exceptional": 0.6, "outstanding": 0.6, "remarkable": 0.5,
    "moon": 0.5, "mooning": 0.6, "calls": 0.3,
}

# Multi-word phrases, keyed by token tuple; matched longest-first and consumed.
_PHRASES: dict[tuple[str, ...], float] = {
    # earnings vs expectations
    ("beat", "estimates"): 1.0, ("beats", "estimates"): 1.0,
    ("beat", "expectations"): 1.0, ("beats", "expectations"): 1.0,
    ("crushed", "estimates"): 1.0, ("smashed", "estimates"): 1.0,
    ("tops", "estimates"): 1.0, ("topped", "estimates"): 1.0,
    ("top", "estimates"): 0.9, ("exceeded", "expectations"): 1.0,
    ("better", "than", "expected"): 0.9, ("beat", "and", "raise"): 1.0,
    ("missed", "estimates"): -1.0, ("misses", "estimates"): -1.0,
    ("missed", "expectations"): -1.0, ("misses", "expectations"): -1.0,
    ("below", "estimates"): -1.0, ("below", "expectations"): -1.0,
    ("worse", "than", "expected"): -0.9, ("falls", "short"): -0.8,
    ("fell", "short"): -0.8,
    # guidance
    ("raised", "guidance"): 1.0, ("raises", "guidance"): 1.0,
    ("guidance", "raised"): 1.0, ("hiked", "guidance"): 1.0,
    ("cut", "guidance"): -1.0, ("cuts", "guidance"): -1.0,
    ("guidance", "cut"): -1.0, ("lowered", "guidance"): -1.0,
    ("lowers", "guidance"): -1.0, ("guidance", "lowered"): -1.0,
    ("withdrew", "guidance"): -1.0, ("withdraws", "guidance"): -1.0,
    # records (the unigram "record" is deliberately not scored)
    ("record", "revenue"): 0.9, ("record", "earnings"): 0.9,
    ("record", "quarter"): 0.9, ("record", "profit"): 0.9,
    ("record", "sales"): 0.9, ("record", "results"): 0.8,
    ("all", "time", "high"): 0.8, ("52", "week", "high"): 0.7,
    ("all", "time", "low"): -0.8, ("52", "week", "low"): -0.7,
    # analyst actions
    ("price", "target", "raised"): 0.8, ("raises", "price", "target"): 0.8,
    ("price", "target", "lowered"): -0.8, ("price", "target", "cut"): -0.8,
    ("cuts", "price", "target"): -0.8,
    ("buy", "rating"): 0.8, ("strong", "buy"): 0.8, ("sell", "rating"): -0.8,
    ("short", "report"): -0.8,
    # FDA / clinical
    ("fda", "approval"): 1.0, ("fda", "approves"): 1.0, ("fda", "approved"): 1.0,
    ("accelerated", "approval"): 0.9, ("breakthrough", "designation"): 0.8,
    ("fast", "track", "designation"): 0.8, ("orphan", "drug", "designation"): 0.6,
    ("met", "primary", "endpoint"): 1.0, ("primary", "endpoint", "met"): 1.0,
    ("missed", "primary", "endpoint"): -1.0, ("failed", "primary", "endpoint"): -1.0,
    ("complete", "response", "letter"): -1.0, ("clinical", "hold"): -0.9,
    ("warning", "letter"): -0.9, ("crl",): -1.0,
    # distress / SEC
    ("going", "concern"): -1.0, ("chapter", "11"): -1.0, ("chapter", "7"): -1.0,
    ("wells", "notice"): -1.0, ("sec", "investigation"): -1.0,
    ("doj", "investigation"): -1.0, ("accounting", "irregularities"): -1.0,
    ("class", "action"): -0.7, ("delisting", "notice"): -0.9,
    # dividends (the bare word is gated as routine; changes are directional)
    ("special", "dividend"): 0.7, ("dividend", "increased"): 0.8,
    ("raises", "dividend"): 0.8, ("dividend", "increase"): 0.7,
    ("cuts", "dividend"): -0.9, ("dividend", "cut"): -0.9,
    ("suspends", "dividend"): -0.9, ("dividend", "suspended"): -0.9,
    # capital structure
    ("public", "offering"): -0.8, ("share", "offering"): -0.8,
    ("direct", "offering"): -0.8, ("secondary", "offering"): -0.7,
    ("registered", "direct"): -0.8, ("reverse", "split"): -0.7,
    ("sell", "off"): -0.6,
    # social slang
    ("short", "squeeze"): 0.7, ("to", "the", "moon"): 0.8,
    ("diamond", "hands"): 0.4, ("going", "long"): 0.5,
    ("rug", "pull"): -0.9, ("pump", "and", "dump"): -0.8,
    ("bag", "holder"): -0.5, ("buying", "calls"): 0.6, ("buying", "puts"): -0.6,
}
_MAX_PHRASE_LEN = max(len(k) for k in _PHRASES)

# Negation: flip the sign of any sentiment hit within the next N tokens,
# dampened - "didn't beat estimates" is bearish, but softer than "missed".
_NEGATORS: frozenset[str] = frozenset({
    "not", "no", "never", "without", "cannot", "cant", "isnt", "wasnt",
    "doesnt", "dont", "wont", "didnt", "couldnt", "unable", "lacks",
    "fails", "failed", "failing", "denies",
})
_NEGATION_WINDOW = 3
_NEGATION_DAMP = 0.75

# Soft-saturation constants for the final score/confidence maps.
_SCORE_TEMPERATURE = 1.5   # tanh scale: one strong hit (+/-1.0) -> score ~ +/-0.58
_CONFIDENCE_MASS = 2.5     # total |weight| at which confidence reaches 1.0

# Evidence gate: a single mild term ("concerns", "launch", |w| <= 0.3) is not
# enough to call direction - the item stays neutral until the total absolute
# weight clears this floor. One moderate/strong term (|w| >= 0.4) passes alone.
_MIN_EVIDENCE = 0.35


def _tokenise(text: str) -> list[str]:
    """Lowercase word/number tokens; apostrophes dropped so "didn't" -> "didnt"."""
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", "").replace("’", ""))


def _score_tokens(
    tokens: list[str],
    unigrams: dict[str, float] = _UNIGRAMS,
) -> tuple[float, float, int]:
    """
    Scan tokens once, matching phrases longest-first (consuming their span),
    then unigrams, applying negation flips. Returns (net, mass, hits) where
    ``net`` is the signed weight sum and ``mass`` the absolute weight sum.
    """
    net = 0.0
    mass = 0.0
    hits = 0
    negate_until = -1
    i = 0
    n = len(tokens)
    while i < n:
        matched_len = 0
        weight: Optional[float] = None
        for length in range(min(_MAX_PHRASE_LEN, n - i), 0, -1):
            w = _PHRASES.get(tuple(tokens[i:i + length]))
            if w is not None:
                weight, matched_len = w, length
                break
        if weight is None:
            tok = tokens[i]
            if tok in _NEGATORS:
                negate_until = i + _NEGATION_WINDOW
                i += 1
                continue
            weight = unigrams.get(tok)
            matched_len = 1
        if weight is not None:
            if i <= negate_until:
                weight = -weight * _NEGATION_DAMP
            net += weight
            mass += abs(weight)
            hits += 1
        i += matched_len
    return net, mass, hits


class LoughranMcDonaldAnalyzer:
    """
    Weighted-lexicon financial sentiment: LM core vocabulary with signed
    per-term weights, finance phrase bigrams/trigrams, and negation handling.
    ~1 ms per item - the social fast path stays lexicon-only by design.

    Scoring
    -------
    net        = sum of signed weights of matched phrases + unigrams
    score      = tanh(net / 1.5)               - soft-saturating, in (-1, 1)
    confidence = min(1.0, sum|weights| / 2.5)  - total evidence mass, not count

    One strong term ("bankruptcy", -1.0) scores ~ -0.58 on its own - enough to
    label clearly bearish - while one mild term ("concerns", -0.3) lands ~ -0.20:
    directionally bearish but low-confidence, so downstream density/confidence
    gates can discount it.

    Parameters
    ----------
    csv_path:
        Optional path to the LM Master Dictionary CSV. Words merge in at +/-0.5;
        built-in hand-tuned weights, phrases, and negation are preserved.
    """

    def __init__(self, csv_path: Optional[str] = None) -> None:
        self._unigrams: dict[str, float] = dict(_UNIGRAMS)
        if csv_path:
            self._load_csv(csv_path)

    def _load_csv(self, path: str) -> None:
        """Merge the full LM master dictionary CSV into the unigram weights."""
        import csv as _csv
        added = 0
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    word = row.get("Word", "").lower().strip()
                    if not word or word in self._unigrams:
                        continue  # hand-tuned weights win
                    if int(row.get("Positive", 0) or 0):
                        self._unigrams[word] = 0.5
                        added += 1
                    elif int(row.get("Negative", 0) or 0):
                        self._unigrams[word] = -0.5
                        added += 1
        except FileNotFoundError:
            log.warning(
                "LM dictionary CSV not found at '%s'; using built-in lexicon only",
                path,
            )
            return
        log.info(
            "LoughranMcDonaldAnalyzer: merged %d words from '%s' (total %d)",
            added, path, len(self._unigrams),
        )

    def _score_text(self, text: str) -> SentimentResult:
        tokens = _tokenise(text[:2048])
        if not tokens:
            return SentimentResult(score=0.0, label="neutral", confidence=0.0)
        net, mass, hits = _score_tokens(tokens, self._unigrams)
        if hits == 0:
            return SentimentResult(score=0.0, label="neutral", confidence=0.0)
        score = math.tanh(net / _SCORE_TEMPERATURE)
        label = _label_from_score(score) if mass >= _MIN_EVIDENCE else "neutral"
        confidence = min(1.0, mass / _CONFIDENCE_MASS)
        return SentimentResult(
            score=round(score, 4),
            label=label,
            confidence=round(confidence, 4),
        )

    def analyze(self, item: NewsItem) -> SentimentResult:
        return self._score_text(_item_text(item, max_chars=2048))

    def analyze_text_batch(
        self, pairs: list[tuple[str, str]]
    ) -> list[SentimentResult]:
        """Score a batch of (title, description) pairs - mirrors FinBERTAnalyzer's interface."""
        return [
            self._score_text(f"{title}. {description}".strip())
            for title, description in pairs
        ]
