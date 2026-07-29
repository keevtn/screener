"""Real-time event bus — Redis pub/sub with in-process + polling fallback.

One channel (``tape:events``) carries small JSON events (new news, fired
catalysts, predictions, grades, ranking runs). Producers call
:func:`publish_event` (never raises; drops silently when Redis is down);
consumers are the APIs' SSE endpoints via :func:`event_stream`, which merges
the Redis subscription with an in-process bus so same-process events (e.g. a
force-run in serve_api) push even with no Redis at all.

Degrade-graceful is the contract: no Redis -> in-process push still works
within each API, and the frontend keeps its polling cadence as the fallback,
so nothing breaks — events only make refreshes instant.

Also here: per-ticker hourly mention counters (INCR + TTL) written at ingest,
feeding the live intraday density endpoint. Redis-only by design (cheap,
ephemeral); callers fall back to client-side bucketing when absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.events")

CHANNEL = "tape:events"
_INTRADAY_PREFIX = "tape:intraday"
_INTRADAY_TTL = 26 * 3600  # a bit over the 24h window the panel shows

# --- Redis client (cached, fail-soft) ----------------------------------------
_client: Any = None
_client_checked_at: float = 0.0
_RETRY_COOLDOWN = 60.0  # after a failed connect, don't re-try every call


def _redis_url() -> str | None:
    return os.environ.get("REDIS_URL") or os.environ.get("REDIS_URI")


def get_redis() -> Any | None:
    """Cached sync Redis client, or None when unconfigured/unreachable.

    A failed connect is remembered for _RETRY_COOLDOWN seconds so hot paths
    (publish per pipeline step) never stack connection timeouts.
    """
    global _client, _client_checked_at
    if _client is not None:
        return _client
    url = _redis_url()
    if not url:
        return None
    now = time.monotonic()
    if _client_checked_at and now - _client_checked_at < _RETRY_COOLDOWN:
        return None
    _client_checked_at = now
    try:
        import redis

        c = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        c.ping()
        _client = c
        return c
    except Exception as exc:  # noqa: BLE001 — any failure means "no redis right now"
        log.info("redis unavailable (%s) — degrading to in-process/polling", type(exc).__name__)
        return None


# --- in-process bus (works with zero Redis) -----------------------------------
_subscribers: set[asyncio.Queue] = set()


def _fanout_local(event: dict[str, Any]) -> None:
    for q in list(_subscribers):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)


def publish_event(type_: str, **payload: Any) -> None:
    """Publish one event. Never raises; Redis-down just means local-only."""
    event = {"type": type_, "at": utcnow().isoformat(), **payload}
    _fanout_local(event)
    r = get_redis()
    if r is not None:
        try:
            r.publish(CHANNEL, json.dumps(event))
        except Exception:  # noqa: BLE001
            global _client
            _client = None  # force re-probe next time (cooldown applies)


async def event_stream() -> AsyncIterator[dict[str, Any]]:
    """Merged stream of in-process + Redis events (for the SSE endpoints).

    Yields event dicts as they arrive. The Redis subscription is optional; the
    in-process queue always works. The caller is responsible for heartbeats.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    stop = asyncio.Event()

    async def _pump_redis() -> None:
        url = _redis_url()
        if not url:
            return
        try:
            import redis.asyncio as aioredis

            r = aioredis.Redis.from_url(url, socket_connect_timeout=3)
            pubsub = r.pubsub()
            await pubsub.subscribe(CHANNEL)
            try:
                while not stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                    if msg and msg.get("type") == "message":
                        with contextlib.suppress(Exception):
                            await q.put(json.loads(msg["data"]))
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.close()
                    await r.aclose()
        except Exception as exc:  # noqa: BLE001 — no redis: local-only stream
            log.info("event_stream: redis subscribe unavailable (%s)", type(exc).__name__)

    pump = asyncio.create_task(_pump_redis())
    try:
        seen: set[str] = set()  # de-dup local+redis copies of the same event
        while True:
            event = await q.get()
            key = f"{event.get('type')}|{event.get('at')}"
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 512:
                seen.clear()
            yield event
    finally:
        stop.set()
        pump.cancel()
        _subscribers.discard(q)


# --- live intraday counters (INCR + TTL at ingest) -----------------------------
def _hour_key(ticker: str, dt: datetime) -> str:
    return f"{_INTRADAY_PREFIX}:{ticker.upper()}:{dt.strftime('%Y%m%d%H')}"


def incr_ticker_hours(tickers: list[str] | tuple[str, ...], when: datetime | None = None) -> None:
    """INCR each ticker's current-hour mention counter (TTL'd). Fail-soft."""
    if not tickers:
        return
    r = get_redis()
    if r is None:
        return
    when = when or utcnow()
    try:
        pipe = r.pipeline(transaction=False)
        for t in tickers:
            key = _hour_key(t, when)
            pipe.incr(key)
            pipe.expire(key, _INTRADAY_TTL)
        pipe.execute()
    except Exception:  # noqa: BLE001
        pass


def intraday_counts(
    ticker: str, *, hours: int = 24, now: datetime | None = None
) -> list[dict[str, Any]] | None:
    """Last `hours` hourly mention counts for a ticker, oldest first.

    Returns None when Redis is unavailable (caller falls back to client-side
    bucketing of /api/news).
    """
    r = get_redis()
    if r is None:
        return None
    now = now or utcnow()
    stamps = [now - timedelta(hours=h) for h in range(hours - 1, -1, -1)]
    try:
        values = r.mget([_hour_key(ticker, s) for s in stamps])
    except Exception:  # noqa: BLE001
        return None
    return [
        {"hour": s.strftime("%Y-%m-%dT%H:00:00Z"), "count": int(v) if v else 0}
        for s, v in zip(stamps, values, strict=True)
    ]
