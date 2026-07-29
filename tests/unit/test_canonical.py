"""Gate 2 task 2.1: canonical model + raw→canonical adapter."""

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.common.models import RawItem
from pipeline.enrich.canonical import from_raw_item, normalize_headline
from pipeline.enrich.canonical import from_values as canonical


def test_normalize_headline():
    assert normalize_headline("Apple Beats, Inc. — Q3!") == "apple beats inc q3"
    assert normalize_headline("  Multiple   Spaces  ") == "multiple spaces"


def test_from_raw_item_round_trips_payload():
    row = RawItem(
        id="abc123",
        source="SEC EDGAR — 8-K",
        source_class="structured",
        url="https://sec/x",
        published_at=datetime(2025, 3, 12, 14, tzinfo=UTC),
        ingested_at=datetime(2025, 3, 12, 15, tzinfo=UTC),
        payload_json={
            "title": "Apple 8-K",
            "description": "d",
            "guid": "g1",
            "extra": {"filing_type": "8-K", "accession_number": "0000-1"},
        },
    )
    item = from_raw_item(row)
    assert item.id == "abc123"
    assert item.title == "Apple 8-K"
    assert item.guid == "g1"
    assert item.is_filing is True
    assert item.filing_type == "8-K"
    assert item.normalized_headline == "apple 8 k"


def test_prose_source_not_flagged_filing():
    item = canonical(
        id="i1",
        source="Business Wire",
        source_class="structured",
        published_at=datetime(2025, 3, 12, 14, tzinfo=UTC),
        title="Company X reports results",
    )
    assert item.is_filing is False
    assert item.filing_type is None
