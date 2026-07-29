"""raw_items sink (docs/ROADMAP.md tasks 1.1 + 1.4).

A dispatcher handler (async ``__call__(item)``) that writes each ingested
NewsItem into ``raw_items`` with the deterministic id ``sha256(source, guid|url)``
and logs ingest latency (``ingested_at − published_at``). Idempotent: the DB
unique constraint on ``raw_items.id`` is the sole dedup authority (the in-process
``_SeenCache`` in backend/ is now only a perf optimization), so a re-seen item
INSERTs nothing via ON CONFLICT DO NOTHING.

The handler writes RAW items only — no sentiment, no enrichment (I2 append-only;
scoring is cluster-scoped in Phase 3). Social items are archived here too (they
are shadow-mode, never scored — see shadow.py), so social id collisions matter.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pipeline.common.events import incr_ticker_hours
from pipeline.common.models import RawItem, raw_item_id
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.ingest.raw_items")

_STRUCTURED_TYPES = {"rss", "sec", "fda"}


class _ItemLike(Protocol):
    source: str
    source_type: str
    title: str
    published_at: datetime
    description: str
    url: str
    extra: dict[str, Any]
    tickers: tuple[str, ...]


def source_class_of(source_type: str) -> str:
    """Map a NewsItem.source_type to raw_items.source_class (I8 discriminator)."""
    return "structured" if source_type in _STRUCTURED_TYPES else "social"


def build_raw_item_values(item: _ItemLike) -> dict[str, Any] | None:
    """NewsItem -> raw_items row values, or None if it has no id material.

    payload_json preserves the original enrichable fields plus the legacy
    content_hash for traceability (gap-report migration note 3).
    """
    extra = getattr(item, "extra", None) or {}
    guid = extra.get("guid") or None
    url = getattr(item, "url", None) or None
    if not guid and not url:
        log.warning("raw_item skipped: no guid or url (source=%s)", getattr(item, "source", "?"))
        return None

    published_at = item.published_at
    if published_at.tzinfo is None:
        log.warning("raw_item skipped: naive published_at (source=%s)", item.source)
        return None

    payload = {
        "title": getattr(item, "title", ""),
        "description": getattr(item, "description", ""),
        "url": url,
        "topic": getattr(item, "topic", ""),
        "tickers": list(getattr(item, "tickers", ()) or ()),
        "extra": extra,
        "content_hash": getattr(item, "content_hash", None),
        "guid": guid,
    }
    return {
        "id": raw_item_id(item.source, guid=guid, url=url),
        "source": item.source,
        "source_class": source_class_of(item.source_type),
        "url": url,
        "published_at": published_at,
        "ingested_at": utcnow(),
        "payload_json": payload,
    }


class RawItemHandler:
    """Dispatcher handler writing NewsItems to raw_items (idempotent)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write(self, item: _ItemLike) -> bool:
        """Insert one item; return True if a new row landed, False if deduped."""
        values = build_raw_item_values(item)
        if values is None:
            return False
        stmt = (
            sqlite_insert(RawItem)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[RawItem.id])
        )
        with Session(self._engine) as session:
            result = session.execute(stmt)
            session.commit()
        inserted = bool(result.rowcount)
        if inserted:
            latency = (values["ingested_at"] - values["published_at"]).total_seconds()
            log.info(
                "raw_item %s source=%s class=%s latency=%.1fs",
                values["id"][:12],
                values["source"],
                values["source_class"],
                latency,
            )
            # Live intraday counters (Redis INCR + TTL, fail-soft no-op without
            # Redis). Only NEW items count — dedups must not inflate density.
            tickers = values["payload_json"].get("tickers") or []
            if tickers:
                incr_ticker_hours(tickers, values["ingested_at"])
        return inserted

    async def __call__(self, item: _ItemLike) -> None:
        # The dispatcher drains sequentially; offload the sync write off the loop.
        await asyncio.to_thread(self.write, item)
