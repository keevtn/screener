"""Supervised Bluesky Jetstream firehose runner (universe-wide cashtag coverage).

A single long-lived subscription to Bluesky's public Jetstream, filtered locally
for real-universe cashtags, writing matches to raw_items shadow-mode. Reconnects
with exponential backoff and cycles Jetstream hosts on failure; writes a
heartbeat file that /health surfaces so a dead stream is visible, not silent.

Degrade-graceful by design: this is best-effort coverage on TOP of the 5-min
term-search sweeps (which keep running and keep the curated non-cashtag phrases),
so the firehose being down never stops social ingestion — it just narrows it.

    python scripts/run_bluesky_firehose.py        # runs until Ctrl-C / killed

Wired into start.ps1 as its own supervised window (the restart-on-crash pattern).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "src", _REPO / "backend"):  # backend/ for NewsItem
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass

import websockets  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from pipeline.common.db import make_engine  # noqa: E402
from pipeline.common.models import Entity  # noqa: E402
from pipeline.enrich.resolve import EntityResolver  # noqa: E402
from pipeline.ingest import RawItemHandler  # noqa: E402
from pipeline.ingest.firehose import (  # noqa: E402
    DEFAULT_STATUS_PATH,
    JETSTREAM_HOSTS,
    FirehoseState,
    Heartbeat,
    consume_stream,
    jetstream_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bluesky_firehose")

STATUS_PATH = DEFAULT_STATUS_PATH


async def supervise(engine, status_path, hosts, *, max_backoff: float = 60.0) -> None:
    with Session(engine) as s:
        entities = s.execute(select(Entity)).scalars().all()
    resolver = EntityResolver(entities)
    log.info("firehose: %d real symbols loaded for cashtag validation", len(entities))
    sink = RawItemHandler(engine)
    hb = Heartbeat(status_path)
    state = FirehoseState()
    hb.write(state, force=True)

    backoff = 1.0
    host_i = 0
    while True:
        host = hosts[host_i % len(hosts)]
        host_i += 1
        try:
            log.info("firehose connecting → %s", host)
            async with websockets.connect(
                jetstream_url(host), max_size=None, ping_interval=20, ping_timeout=20
            ) as ws:
                backoff = 1.0  # a good connection resets the backoff
                await consume_stream(ws, resolver, sink, hb, state)
            # Clean generator end (server closed) — treat as a reconnect.
            state.connected = False
            state.last_error = "stream ended"
        except Exception as exc:  # noqa: BLE001 — the supervisor never dies on a stream fault
            state.connected = False
            state.reconnects += 1
            state.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning(
                "firehose disconnected (%s) — reconnecting in %.0fs (events=%d matches=%d)",
                type(exc).__name__, backoff, state.events, state.matches,
            )
        hb.write(state, force=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


def main() -> None:
    try:
        asyncio.run(supervise(make_engine(), STATUS_PATH, JETSTREAM_HOSTS))
    except KeyboardInterrupt:
        log.info("firehose stopped")


if __name__ == "__main__":
    main()
