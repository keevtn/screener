"""Task 0.2 gate test: raw_items UPDATE/DELETE raises, via ORM and raw SQL (I2)."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from pipeline.common.models import AppendOnlyViolation, RawItem, raw_item_id

PUBLISHED = datetime(2025, 3, 12, 14, 30, tzinfo=UTC)


def _insert_item(session: Session) -> RawItem:
    item = RawItem(
        id=raw_item_id("TestWire", url="https://example.com/story-1"),
        source="TestWire",
        source_class="structured",
        url="https://example.com/story-1",
        published_at=PUBLISHED,
        ingested_at=PUBLISHED,
        payload_json={"title": "hello"},
    )
    session.add(item)
    session.commit()
    return item


def test_orm_update_raises(session):
    item = _insert_item(session)
    item.source = "EditedWire"
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()


def test_orm_delete_raises(session):
    item = _insert_item(session)
    session.delete(item)
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()


def test_raw_sql_update_blocked_by_trigger(engine, session):
    _insert_item(session)
    with pytest.raises(sa.exc.DatabaseError, match="append-only"), engine.begin() as conn:
        conn.execute(sa.text("UPDATE raw_items SET source = 'sneaky'"))
    with engine.connect() as conn:
        source = conn.execute(sa.text("SELECT source FROM raw_items")).scalar_one()
    assert source == "TestWire"


def test_raw_sql_delete_blocked_by_trigger(engine, session):
    _insert_item(session)
    with pytest.raises(sa.exc.DatabaseError, match="append-only"), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM raw_items"))
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM raw_items")).scalar_one()
    assert count == 1
