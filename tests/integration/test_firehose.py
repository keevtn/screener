"""Bluesky Jetstream firehose: cashtag filter, stream loop, dedup, heartbeat."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

# backend/ on the path for the lazily-imported NewsItem (as the runner arranges).
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from pipeline.common.models import RawItem  # noqa: E402
from pipeline.enrich.resolve import EntityResolver  # noqa: E402
from pipeline.ingest import RawItemHandler  # noqa: E402
from pipeline.ingest.firehose import (  # noqa: E402
    FirehoseState,
    Heartbeat,
    consume_stream,
    liveness,
    post_to_item,
    read_status,
)

ENTITIES = [
    {"ticker": "AAPL", "canonical_name": "Apple Inc.", "aliases_json": ["Apple"]},
    {"ticker": "NVDA", "canonical_name": "NVIDIA Corp", "aliases_json": ["Nvidia"]},
]
RESOLVER = EntityResolver(ENTITIES)


def _evt(text, *, did="did:plc:abc", rkey="r1", op="create", coll="app.bsky.feed.post", kind="commit"):
    return json.dumps({
        "kind": kind, "did": did,
        "commit": {"operation": op, "collection": coll, "rkey": rkey,
                   "record": {"text": text, "createdAt": "2026-07-19T12:00:00.000Z"}},
    })


class FakeWS:
    """Async-iterable stand-in for a connected websocket."""

    def __init__(self, msgs):
        self._msgs = list(msgs)

    def __aiter__(self):
        async def gen():
            for m in self._msgs:
                yield m
        return gen()


# --- pure event → item -------------------------------------------------------


def test_post_to_item_cashtag_match():
    item = post_to_item(json.loads(_evt("loading $AAPL calls")), RESOLVER)
    assert item is not None
    assert item.source == "Bluesky" and item.source_type == "social"
    assert item.extra["cashtags"] == ["AAPL"] and item.extra["via"] == "firehose"
    assert item.extra["guid"] == "at://did:plc:abc/app.bsky.feed.post/r1"
    assert item.published_at.tzinfo is not None


def test_post_to_item_filters():
    # no cashtag, non-universe cashtag, delete op, non-post collection, non-commit
    assert post_to_item(json.loads(_evt("just vibing today")), RESOLVER) is None
    assert post_to_item(json.loads(_evt("$YOLO to the moon")), RESOLVER) is None
    assert post_to_item(json.loads(_evt("$AAPL", op="delete")), RESOLVER) is None
    assert post_to_item(json.loads(_evt("$AAPL", coll="app.bsky.feed.like")), RESOLVER) is None
    assert post_to_item(json.loads(_evt("$AAPL", kind="account")), RESOLVER) is None


# --- stream loop -------------------------------------------------------------


def test_consume_stream_writes_matches_and_dedups(engine, tmp_path):
    sink = RawItemHandler(engine)
    hb = Heartbeat(tmp_path / "hb.json", min_interval_s=0)
    state = FirehoseState()
    msgs = [
        _evt("$AAPL breakout", rkey="a1"),
        _evt("no ticker here"),
        _evt("$NVDA earnings", rkey="n1"),
        _evt("$AAPL breakout", rkey="a1"),  # duplicate URI -> deduped
    ]
    asyncio.run(consume_stream(FakeWS(msgs), RESOLVER, sink, hb, state))
    assert state.events == 4 and state.matches == 2  # AAPL + NVDA, dup dropped
    with Session(engine) as s:
        rows = s.execute(select(RawItem)).scalars().all()
        assert len(rows) == 2
        assert {r.source_class for r in rows} == {"social"}  # I8 shadow lane
        assert {r.source for r in rows} == {"Bluesky"}
    assert (tmp_path / "hb.json").exists()  # heartbeat written


# --- heartbeat + liveness (feeds /health) ------------------------------------


def test_heartbeat_and_liveness(tmp_path):
    clock = [1000.0]
    hb = Heartbeat(tmp_path / "s.json", min_interval_s=5, clock=lambda: clock[0])
    st = FirehoseState()
    st.connected = True
    st.events = 10
    st.matches = 3
    st.last_event_at = 1000.0
    hb.write(st, force=True)

    status = read_status(tmp_path / "s.json")
    assert status["connected"] is True and status["matches"] == 3

    fresh = liveness(status, now=1001.0)  # 1s later
    assert fresh["alive"] is True and fresh["present"] is True

    stale = liveness(status, now=1000.0 + 200)  # >90s since heartbeat/event
    assert stale["alive"] is False

    absent = liveness(None)
    assert absent["present"] is False and absent["alive"] is False


def test_heartbeat_throttles(tmp_path):
    clock = [0.0]
    hb = Heartbeat(tmp_path / "t.json", min_interval_s=5, clock=lambda: clock[0])
    st = FirehoseState()
    hb.write(st)  # writes at t=0
    first = (tmp_path / "t.json").stat().st_mtime_ns
    st.events = 99
    clock[0] = 2.0
    hb.write(st)  # throttled (2s < 5s) -> file unchanged
    assert read_status(tmp_path / "t.json")["events"] == 0
    clock[0] = 6.0
    hb.write(st)  # past the interval -> writes
    assert read_status(tmp_path / "t.json")["events"] == 99
    _ = first
