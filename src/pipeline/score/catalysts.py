"""Rules-based catalyst classifier (docs/ROADMAP.md task 3.2, invariant I11).

Fully config-driven: the taxonomy lives in configs/catalysts.yaml and this engine
interprets generic detection rules (SEC filing types, EDGAR item codes, keyword
subsets). Adding or tuning a catalyst type requires zero code changes here — a new
type in the YAML is detected on its next run (see test_taxonomy_zero_code).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pipeline.common.config_files import configs_dir
from pipeline.enrich.canonical import CanonicalItem, normalize_headline

# Stage/direction keyword-subset names and what they imply.
_STAGE_SUBSETS = {"keywords_rumor": "rumor", "keywords_announced": "announced"}
_DIRECTION_SUBSETS = {"keywords_bullish": "bullish", "keywords_bearish": "bearish"}
_PLAIN_KEYWORD_KEYS = ("keywords",)


@dataclass(frozen=True)
class CatalystResult:
    catalyst_type: str
    event_stage: str | None
    materiality: float
    direction_hint: str | None
    high_alert: bool
    predictive: bool
    reaction_dependent: bool


class CatalystTaxonomy:
    def __init__(self, spec: dict[str, Any]) -> None:
        self._catalysts: list[tuple[str, dict[str, Any]]] = list(
            (spec.get("catalysts", {}) or {}).items()
        )
        self._by_name = dict(self._catalysts)
        self.high_alert_cutoff = float(spec.get("high_alert_cutoff", 0.70))

    def direction_for(self, type_name: str, text: str) -> str | None:
        """Direction implied by a named type's bullish/bearish keyword subsets.

        Used by the earnings-surprise guard (3.4) to let guidance language override
        results-level text direction, without hardcoding the keywords.
        """
        cat = self._by_name.get(type_name)
        if not cat:
            return None
        det = cat.get("detection", {}) or {}
        norm = normalize_headline(text)
        for key, implied in _DIRECTION_SUBSETS.items():
            if _any_kw(det.get(key), norm):
                return implied
        return None

    def classify(self, item: CanonicalItem) -> CatalystResult | None:
        filing_type = (item.filing_type or "").strip().upper()
        # Hard signals (filing type / EDGAR item code) scan the full body — item
        # codes live in the filing text. Keyword signals match the HEADLINE only:
        # a real catalyst leads the headline, whereas an incidental "to acquire" in
        # a macro-stats body is not one (kills body-text false positives).
        full_text = normalize_headline(f"{item.title} {item.description}")
        title_text = normalize_headline(item.title)
        # Two passes so a reliable hard signal always beats a mere keyword match on
        # an earlier-listed type — e.g. an S-1 ("initial public offering") is `ipo`,
        # not `secondary_offering` on the substring "public offering".
        for name, cat in self._catalysts:
            det = cat.get("detection", {}) or {}
            if _hard_match(det, filing_type, full_text):
                return self._build(name, cat, det, title_text, hard=True)
        for name, cat in self._catalysts:
            det = cat.get("detection", {}) or {}
            if _keyword_match(det, title_text):
                return self._build(name, cat, det, title_text, hard=False)
        return None

    def _build(
        self, name: str, cat: dict[str, Any], det: dict[str, Any], text: str, *, hard: bool
    ) -> CatalystResult:
        materiality = float(cat.get("default_materiality", 0.5))
        # Direction: a matched bullish/bearish keyword subset overrides the base hint.
        direction = cat.get("direction_hint")
        for key, implied in _DIRECTION_SUBSETS.items():
            if _any_kw(det.get(key), text):
                direction = implied
                break
        # Stage: rumor keywords -> rumor; announced keywords or a hard filing/item
        # signal -> announced; else the type's first declared stage.
        stages = cat.get("stages") or []
        if _any_kw(det.get("keywords_rumor"), text):
            stage = "rumor"
        elif hard or _any_kw(det.get("keywords_announced"), text):
            stage = "announced"
        else:
            stage = stages[0] if stages else None
        if stages and stage not in stages:  # keep stage valid for this type
            stage = stages[0]
        return CatalystResult(
            catalyst_type=name,
            event_stage=stage,
            materiality=materiality,
            direction_hint=direction,
            high_alert=materiality >= self.high_alert_cutoff,
            predictive=bool(cat.get("predictive", True)),
            reaction_dependent=bool(cat.get("reaction_dependent", False)),
        )


def _any_kw(keywords: Any, text: str) -> bool:
    if not keywords:
        return False
    return any(normalize_headline(k) in text for k in keywords)


def _hard_match(det: dict[str, Any], filing_type: str, text: str) -> bool:
    """A reliable signal: SEC filing type or EDGAR item code."""
    for ft in det.get("filing_types", []) or []:
        if filing_type and filing_type == ft.strip().upper():
            return True
    for item in det.get("edgar_items", []) or []:
        code = normalize_headline(item)
        if code and re.search(rf"item\s+{re.escape(code)}\b", text):
            return True
    return False


def _keyword_match(det: dict[str, Any], text: str) -> bool:
    return any(
        _any_kw(det.get(key), text)
        for key in (*_PLAIN_KEYWORD_KEYS, *_STAGE_SUBSETS, *_DIRECTION_SUBSETS)
    )


def load_taxonomy(path: str | Path | None = None) -> CatalystTaxonomy:
    p = Path(path) if path else configs_dir() / "catalysts.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return CatalystTaxonomy(spec)


def classify_catalyst(
    item: CanonicalItem, taxonomy: CatalystTaxonomy | None = None
) -> CatalystResult | None:
    return (taxonomy or load_taxonomy()).classify(item)
