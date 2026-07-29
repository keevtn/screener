"""Bluesky Jetstream firehose → universe-wide cashtag coverage (shadow-mode).

The term-search Bluesky lane (dispatch.py) polls a curated set of hashtags every
5 min. This complements it: a single long-lived subscription to Bluesky's public
Jetstream (JSON commit stream — the pragmatic firehose, no CBOR/CAR decoding),
filtered LOCALLY for cashtags that resolve to a real ticker in the entities
universe (blocklist + common-word guarded via EntityResolver.cashtag_tickers).
Matches land in raw_items shadow-mode (I8), deduped by post URI with the
term-search lane (same source label + guid).

Degrade-graceful: the firehose is best-effort. If it's down, the term-search
sweeps still run (and keep the curated non-cashtag phrases). A heartbeat file
makes a dead stream VISIBLE on /health rather than silent — continuity is the
Phase-6 baseline clock, so an outage must be loud.

This module is the testable core (pure event→item, the stream loop, heartbeat);
scripts/run_bluesky_firehose.py is the supervised runner.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("pipeline.ingest.firehose")

WANTED_COLLECTION = "app.bsky.feed.post"
# Heartbeat file the runner writes and /health reads (repo-root/data/…).
DEFAULT_STATUS_PATH = Path(__file__).resolve().parents[3] / "data" / "bluesky_firehose.json"
# Jetstream public instances (JSON). We try them in order on reconnect.
JETSTREAM_HOSTS = (
    "wss://jetstream2.us-east.bsky.network/subscribe",
    "wss://jetstream1.us-east.bsky.network/subscribe",
    "wss://jetstream2.us-west.bsky.network/subscribe",
    "wss://jetstream1.us-west.bsky.network/subscribe",
)


def jetstream_url(host: str) -> str:
    return f"{host}?wantedCollections={WANTED_COLLECTION}"


def _post_web_url(did: str, rkey: str) -> str:
    return f"https://bsky.app/profile/{did}/post/{rkey}" if did and rkey else ""


def _parse_created(s: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)
    except Exception:  # noqa: BLE001 — malformed timestamp -> stamp on ingest
        return datetime.now(UTC)


def post_to_item(evt: dict[str, Any], resolver: Any) -> Any | None:
    """A Jetstream commit event -> NewsItem iff its text names a real-universe
    cashtag, else None. Pure and testable; NewsItem imported lazily so the module
    loads without backend/ on the path."""
    if evt.get("kind") != "commit":
        return None
    commit = evt.get("commit") or {}
    if commit.get("operation") != "create" or commit.get("collection") != WANTED_COLLECTION:
        return None
    record = commit.get("record") or {}
    text = (record.get("text") or "").strip()
    if not text:
        return None
    tickers = resolver.cashtag_tickers(text)
    if not tickers:
        return None  # not about a real ticker -> drop (the firehose filter)

    from IngestionModule import NewsItem  # backend/ on sys.path via the runner

    did = evt.get("did") or ""
    rkey = commit.get("rkey") or ""
    uri = f"at://{did}/{WANTED_COLLECTION}/{rkey}" if did and rkey else _post_web_url(did, rkey)
    return NewsItem(
        source="Bluesky",  # same label as the term-search lane -> cross-lane dedup
        source_type="social",
        title=text[:120] + ("…" if len(text) > 120 else ""),
        published_at=_parse_created(record.get("createdAt")),
        description=text,
        url=_post_web_url(did, rkey),
        extra={"guid": uri, "bsky_did": did, "cashtags": tickers, "via": "firehose"},
    )


@dataclass
class FirehoseState:
    events: int = 0
    matches: int = 0
    reconnects: int = 0
    connected: bool = False
    last_event_at: float | None = None  # epoch seconds
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)


class Heartbeat:
    """Writes the firehose liveness snapshot to a JSON file that /health reads.
    Throttled so a busy stream doesn't hammer the disk."""

    def __init__(self, path: str | Path, *, min_interval_s: float = 5.0, clock=time.time) -> None:
        self._path = Path(path)
        self._min = min_interval_s
        self._clock = clock
        self._last = float("-inf")  # first write always goes through

    def write(self, state: FirehoseState, *, force: bool = False) -> None:
        now = self._clock()
        if not force and (now - self._last) < self._min:
            return
        self._last = now
        payload = {
            "updated_at": now,
            "connected": state.connected,
            "last_event_at": state.last_event_at,
            "events": state.events,
            "matches": state.matches,
            "reconnects": state.reconnects,
            "last_error": state.last_error,
            "started_at": state.started_at,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)  # atomic swap so /health never reads a half-write
        except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort
            log.warning("firehose heartbeat write failed: %s", exc)


async def consume_stream(ws: Any, resolver: Any, sink: Any, hb: Heartbeat, state: FirehoseState) -> None:
    """Drain one connected stream: parse → cashtag-filter → write → heartbeat.
    A per-message error never breaks the stream; the caller handles disconnects."""
    state.connected = True
    hb.write(state, force=True)
    async for raw in ws:
        state.events += 1
        state.last_event_at = time.time()
        try:
            evt = json.loads(raw)
            item = post_to_item(evt, resolver)
            if item is not None:
                state.matches += sink.write(item)
        except Exception as exc:  # noqa: BLE001 — one bad frame must not drop the stream
            log.debug("firehose frame skipped: %s", type(exc).__name__)
        hb.write(state)


def read_status(path: str | Path) -> dict[str, Any] | None:
    """Read the heartbeat file (for /health). None if absent/unreadable."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return None


def liveness(status: dict[str, Any] | None, *, now: float | None = None, stale_s: float = 90.0) -> dict[str, Any]:
    """Derive an alive/stale verdict for /health from a heartbeat snapshot."""
    now = now if now is not None else time.time()
    if not status:
        return {"present": False, "alive": False, "note": "no firehose heartbeat"}
    updated_age = now - float(status.get("updated_at") or 0)
    ev_age = status.get("last_event_at")
    ev_age = (now - float(ev_age)) if ev_age else None
    alive = bool(status.get("connected")) and updated_age < stale_s and (ev_age is None or ev_age < stale_s)
    return {
        "present": True,
        "alive": alive,
        "connected": bool(status.get("connected")),
        "heartbeat_age_s": round(updated_age, 1),
        "last_event_age_s": round(ev_age, 1) if ev_age is not None else None,
        "events": status.get("events"),
        "matches": status.get("matches"),
        "reconnects": status.get("reconnects"),
        "last_error": status.get("last_error"),
    }
