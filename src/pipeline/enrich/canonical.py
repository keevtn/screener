"""Canonical item model + raw→canonical adapters (docs/ROADMAP.md task 2.1).

`raw_items` rows store the original payload verbatim (I2 append-only). Enrichment
works on a typed, normalized view of them — the CanonicalItem — so clustering,
tiering, and entity resolution never re-parse payload dicts by hand.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from pipeline.common.models import RawItem

# Structured document sources whose text is a filing/press release rather than
# journalistic prose (drives L-M vs FinBERT weighting later, task 3.3).
_FILING_SOURCE_RE = re.compile(r"^SEC EDGAR|^FDA ", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_headline(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the fuzzy-match key."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", title.lower())).strip()


class CanonicalItem(BaseModel):
    """A normalized, enrichment-ready view of a raw_items row."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: str
    source_class: Literal["structured", "social"]
    url: str | None = None
    published_at: AwareDatetime
    ingested_at: AwareDatetime
    title: str = ""
    description: str = ""
    guid: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def normalized_headline(self) -> str:
        return normalize_headline(self.title)

    @property
    def is_filing(self) -> bool:
        """True for primary-document sources (EDGAR/FDA) — filing text, not prose."""
        return bool(_FILING_SOURCE_RE.match(self.source))

    @property
    def filing_type(self) -> str | None:
        return self.extra.get("filing_type")

    @property
    def text_for_matching(self) -> str:
        return self.normalized_headline


def from_raw_item(row: RawItem) -> CanonicalItem:
    """Adapt a persisted RawItem into a CanonicalItem (payload_json → typed view)."""
    payload = row.payload_json or {}
    return CanonicalItem(
        id=row.id,
        source=row.source,
        source_class=row.source_class,
        url=row.url,
        published_at=row.published_at,
        ingested_at=row.ingested_at,
        title=payload.get("title", ""),
        description=payload.get("description", ""),
        guid=payload.get("guid"),
        extra=payload.get("extra") or {},
    )


def from_values(**kwargs: Any) -> CanonicalItem:
    """Build a CanonicalItem directly (tests / adapters that don't hit the DB)."""
    kwargs.setdefault("ingested_at", kwargs.get("published_at"))
    if isinstance(kwargs.get("published_at"), datetime):
        pass
    return CanonicalItem(**kwargs)
