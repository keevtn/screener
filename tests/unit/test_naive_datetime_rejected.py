"""Task 0.2 gate test: naive datetimes rejected at every boundary (I1)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pipeline.common.models import RawItem, raw_item_id
from pipeline.common.schemas import PredictionIn, RawItemIn
from pipeline.common.timeutil import ensure_utc

NAIVE = datetime(2025, 3, 12, 14, 30)  # no tzinfo
AWARE = datetime(2025, 3, 12, 14, 30, tzinfo=UTC)


def test_pydantic_rejects_naive_raw_item():
    with pytest.raises(ValidationError):
        RawItemIn(
            source="TestWire",
            source_class="structured",
            url="https://example.com/x",
            published_at=NAIVE,
        )


def test_pydantic_rejects_naive_prediction():
    with pytest.raises(ValidationError):
        PredictionIn(
            ticker="AAPL",
            direction="bullish",
            confidence=0.5,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=NAIVE,
            config_version="v0",
        )


def test_pydantic_accepts_aware():
    item = RawItemIn(
        source="TestWire",
        source_class="structured",
        url="https://example.com/x",
        published_at=AWARE,
    )
    assert item.published_at.tzinfo is not None


def test_orm_bind_rejects_naive(session):
    session.add(
        RawItem(
            id=raw_item_id("TestWire", url="https://example.com/naive"),
            source="TestWire",
            source_class="structured",
            url="https://example.com/naive",
            published_at=NAIVE,  # sneaks past pydantic straight into the ORM
            ingested_at=AWARE,
        )
    )
    with pytest.raises(Exception, match="naive datetime rejected"):
        session.commit()
    session.rollback()


def test_ensure_utc_rejects_naive():
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(NAIVE)
    assert ensure_utc(AWARE) == AWARE
