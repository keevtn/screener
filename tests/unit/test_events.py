"""Real-time event bus: in-process pub/sub, no-Redis fallbacks, intraday counters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

import pipeline.common.events as ev

NOW = datetime(2026, 7, 14, 15, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Isolate every test from the env's real REDIS_URL and reset the client cache."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URI", raising=False)
    monkeypatch.setattr(ev, "_client", None)
    monkeypatch.setattr(ev, "_client_checked_at", 0.0)


def test_no_redis_fallbacks():
    assert ev.get_redis() is None
    assert ev.intraday_counts("AAPL") is None
    ev.publish_event("news", count=1)  # must not raise with no redis and no subscribers
    ev.incr_ticker_hours(["AAPL"])  # ditto


def test_inprocess_pubsub_roundtrip():
    async def main():
        agen = ev.event_stream()
        task = asyncio.create_task(asyncio.wait_for(anext(agen), timeout=3))
        await asyncio.sleep(0.05)  # let the stream register its queue
        ev.publish_event("fired", count=3)
        got = await task
        await agen.aclose()
        return got

    got = asyncio.run(main())
    assert got["type"] == "fired" and got["count"] == 3 and "at" in got


class FakeRedis:
    """Minimal stub covering the counter paths (pipeline/incr/expire/mget)."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    def pipeline(self, transaction=False):
        return _FakePipe(self)

    def mget(self, keys):
        return [self.store.get(k) for k in keys]


class _FakePipe:
    def __init__(self, r: FakeRedis):
        self.r = r

    def incr(self, key):
        self.r.store[key] = self.r.store.get(key, 0) + 1

    def expire(self, key, ttl):
        self.r.expires[key] = ttl

    def execute(self):
        pass


def test_intraday_counters_key_math(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ev, "get_redis", lambda: fake)

    ev.incr_ticker_hours(["aapl", "MSFT"], NOW)
    ev.incr_ticker_hours(["AAPL"], NOW)
    assert fake.store == {
        "tape:intraday:AAPL:2026071415": 2,  # lowercase input normalized, double-counted
        "tape:intraday:MSFT:2026071415": 1,
    }
    assert all(t == 26 * 3600 for t in fake.expires.values())

    counts = ev.intraday_counts("AAPL", hours=3, now=NOW)
    assert [c["count"] for c in counts] == [0, 0, 2]  # zero-filled, oldest first
    assert counts[-1]["hour"] == "2026-07-14T15:00:00Z"


def test_intraday_counts_none_without_redis(monkeypatch):
    monkeypatch.setattr(ev, "get_redis", lambda: None)
    assert ev.intraday_counts("AAPL") is None
