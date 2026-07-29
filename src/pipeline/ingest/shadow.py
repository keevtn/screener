"""Shadow-mode guard (docs/ROADMAP.md task 1.5, invariant I8).

Social sources are archived to raw_items but must never be scored or reach
predictions until Phase 6. Every scorer-facing read of raw_items MUST go through
this single helper, so the exclusion of ``source_class='social'`` lives in exactly
one place (enforced in code, not convention).
"""

from __future__ import annotations

from sqlalchemy import Select, select

from pipeline.common.models import RawItem

SOCIAL_CLASS = "social"


def scorer_visible_stmt() -> Select:
    """A SELECT over raw_items that scorers may consume — social excluded (I8)."""
    return select(RawItem).where(RawItem.source_class != SOCIAL_CLASS)
