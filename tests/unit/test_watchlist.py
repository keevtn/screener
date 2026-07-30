"""TRADER watchlist lane: pin CRUD + enrichment (armed / scheduled / watching).

Uses the real SQLite `session` fixture from conftest. No network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from pipeline.api import watchlist
from pipeline.common.models import ArmedState, Cluster, RawItem, ScheduledEvent


def test_pin_list_unpin(session):
    watchlist.add_pin(session, "aapl")  # lower-cased -> normalized
    watchlist.add_pin(session, "MSFT", note="earnings play")
    pins = watchlist.list_pins(session)
    assert {p.ticker for p in pins} == {"AAPL", "MSFT"}
    assert any(p.note == "earnings play" for p in pins)

    # idempotent re-pin updates the note, doesn't duplicate
    watchlist.add_pin(session, "AAPL", note="new note")
    assert len(watchlist.list_pins(session)) == 2

    assert watchlist.remove_pin(session, "aapl") is True
    assert watchlist.remove_pin(session, "AAPL") is False  # already gone
    assert {p.ticker for p in watchlist.list_pins(session)} == {"MSFT"}


def test_watchlist_view_watching_state(session):
    watchlist.add_pin(session, "NVDA")
    v = watchlist.watchlist_view(session)
    assert v["count"] == 1
    item = v["items"][0]
    assert item["ticker"] == "NVDA"
    assert item["state"] == "watching"


def test_watchlist_view_armed_state(session):
    now = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
    session.add(RawItem(
        id="raw1", source="Reuters", source_class="structured", url="https://ex.com/a",
        published_at=now, ingested_at=now, payload_json={"title": "PROC beats on earnings"},
    ))
    # Flush parents (raw_items -> clusters) before the ArmedState child: FKs are
    # enforced and there are no ORM relationships to order the flush.
    session.flush()
    session.add(Cluster(cluster_id="c1", origin_item_id="raw1", member_count=1, created_at=now))
    session.flush()
    session.add(ArmedState(
        ticker="PROC", cluster_id="c1", catalyst_type="earnings", event_ts=now,
        armed_at=now, status="armed", created_at=now,
    ))
    session.flush()
    watchlist.add_pin(session, "PROC")
    session.commit()

    v = watchlist.watchlist_view(session)
    item = next(i for i in v["items"] if i["ticker"] == "PROC")
    assert item["state"] == "armed"
    assert "earnings" in item["state_label"]
    assert item["armed"]["catalyst_type"] == "earnings"


def test_watchlist_view_scheduled_state(session):
    session.add(ScheduledEvent(
        ticker="BIO", catalyst_type="fda_pdufa", event_date=date(2099, 1, 15),
        source="test", status="upcoming", created_at=datetime(2026, 7, 20, tzinfo=UTC),
    ))
    watchlist.add_pin(session, "BIO")
    session.commit()

    v = watchlist.watchlist_view(session)
    item = next(i for i in v["items"] if i["ticker"] == "BIO")
    assert item["state"] == "scheduled"
    assert item["scheduled"]["event_date"] == "2099-01-15"
