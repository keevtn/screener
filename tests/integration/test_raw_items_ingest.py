"""Gate 1 tasks 1.1/1.4/1.5: raw_items sink, idempotency, golden feeds, shadow guard."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import RawItem, raw_item_id
from pipeline.ingest import RawItemHandler, build_raw_item_values, source_class_of
from pipeline.ingest.shadow import scorer_visible_stmt

# The backend extractors use flat imports and run from backend/.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


class FakeHttp:
    """Stand-in for _HttpClient that returns fixture bytes (no network)."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def get_text_and_headers(self, url, headers=None):
        return self._text, {}

    async def get_text(self, url, headers=None):
        return self._text

    async def get_json(self, url, params=None):
        return {}


class _Item:
    """Minimal NewsItem-like for direct sink tests."""

    def __init__(self, source, source_type, url, guid=None, published_at=None):
        self.source = source
        self.source_type = source_type
        self.title = "t"
        self.description = "d"
        self.url = url
        self.extra = {"guid": guid} if guid else {}
        self.topic = ""
        self.tickers = ()
        self.published_at = published_at or datetime(2025, 3, 12, 14, 0, tzinfo=UTC)
        self.content_hash = "legacy-hash"


# --- source_class + id -------------------------------------------------------


def test_source_class_mapping():
    assert source_class_of("rss") == "structured"
    assert source_class_of("sec") == "structured"
    assert source_class_of("fda") == "structured"
    assert source_class_of("social") == "social"


def test_build_values_prefers_guid_over_url():
    v = build_raw_item_values(_Item("Wire", "rss", "https://x/a", guid="g-1"))
    assert v["id"] == raw_item_id("Wire", guid="g-1")
    assert v["source_class"] == "structured"
    assert v["payload_json"]["content_hash"] == "legacy-hash"


def test_build_values_none_without_guid_or_url():
    assert build_raw_item_values(_Item("Wire", "rss", "", guid=None)) is None


# --- 1.1 idempotency ---------------------------------------------------------


def test_raw_item_idempotent(engine):
    handler = RawItemHandler(engine)
    item = _Item("Wire", "rss", "https://x/a", guid="g-1")
    assert handler.write(item) is True
    assert handler.write(item) is False  # ON CONFLICT DO NOTHING
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(RawItem)).scalar_one() == 1


def test_distinct_guids_are_distinct_rows(engine):
    handler = RawItemHandler(engine)
    # Same templated URL (the FDA drug-events hazard) but distinct guids -> 2 rows.
    handler.write(_Item("FDA Adverse Events", "fda", "https://fda/daf", guid="report-1"))
    handler.write(_Item("FDA Adverse Events", "fda", "https://fda/daf", guid="report-2"))
    with Session(engine) as s:
        assert s.execute(select(func.count()).select_from(RawItem)).scalar_one() == 2


# --- 1.5 shadow-mode guard ---------------------------------------------------


def test_shadow_mode_excludes_social(engine):
    handler = RawItemHandler(engine)
    handler.write(_Item("Wire", "rss", "https://x/a", guid="s1"))
    handler.write(_Item("Reddit - wsb", "social", "https://reddit/p", guid="soc1"))
    with Session(engine) as s:
        visible = s.execute(scorer_visible_stmt()).scalars().all()
        all_rows = s.execute(select(RawItem)).scalars().all()
    assert len(all_rows) == 2  # social IS archived (shadow mode)
    assert [r.source_class for r in visible] == ["structured"]  # but never scorer-visible


# --- 1.1 golden feeds -> raw_items (drives the real parsers + guid threading) -


def _poll(coro):
    return asyncio.run(coro)


def test_feed_parse_golden_rss(engine):
    from IngestionModule import RSSExtractor

    ext = RSSExtractor()
    cfg = {"label": "Test Newswire", "url": "http://x", "source_type": "rss"}
    items = _poll(ext._poll_feed(cfg, FakeHttp((FEEDS / "sample_rss.xml").read_text())))
    assert len(items) == 2
    assert items[0].extra["guid"] == "guid-rss-apple-1"

    handler = RawItemHandler(engine)
    assert sum(handler.write(i) for i in items) == 2
    with Session(engine) as s:
        expected = raw_item_id("Test Newswire", guid="guid-rss-apple-1")
        row = s.get(RawItem, expected)
        assert row is not None
        assert row.source_class == "structured"
        assert row.published_at.tzinfo is not None


def test_feed_parse_golden_sec(engine):
    from IngestionModule import SECExtractor

    ext = SECExtractor()
    items = _poll(ext._poll_filing_type("8-K", FakeHttp((FEEDS / "sample_sec.xml").read_text())))
    assert len(items) == 1
    item = items[0]
    assert item.source == "SEC EDGAR — 8-K"
    assert item.extra["accession_number"] == "0000320193-25-000123"
    assert "0000320193-25-000123" in item.extra["guid"]

    handler = RawItemHandler(engine)
    assert handler.write(item) is True


def test_feed_parse_golden_fda(engine):
    from IngestionModule import FDAExtractor

    ext = FDAExtractor()
    items = _poll(
        ext._poll_rss(FakeHttp((FEEDS / "sample_fda.xml").read_text()), "http://x", "FDA Press")
    )
    assert len(items) == 1
    assert items[0].extra["guid"] == "fda-press-xyz-1"
    assert RawItemHandler(engine).write(items[0]) is True


def test_naive_published_at_skipped(engine):
    item = _Item("Wire", "rss", "https://x/a", guid="g-naive")
    item.published_at = datetime(2025, 3, 12, 14, 0)  # naive -> rejected (I1)
    assert RawItemHandler(engine).write(item) is False
