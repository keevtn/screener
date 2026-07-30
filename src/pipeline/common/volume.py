"""Persistent-volume guard.

During a Railway deploy cutover a temporary container can run the full stack
against an EPHEMERAL, just-hydrated SQLite DB (same service env — Alpaca keys, the
driver flag — but the volume not actually mounted). Harmless for predictions, but
catastrophic for the trader: it would place REAL paper orders while recording
sim_trades to a DB that evaporates — orphaned positions, broken provenance, and a
second driver the heartbeat's double-driver check can't even see (different DBs).

The reliable signal is a positive filesystem proof: does the DB directory live on
a DIFFERENT device than the container's overlay root? A real mounted volume is a
separate filesystem (``st_dev`` differs from ``/``); the ephemeral overlay is not.
This is stronger than ``os.path.ismount`` alone and works for subpaths of the
mount, and it corroborates Railway's own ``RAILWAY_VOLUME_MOUNT_PATH`` signal.

Policy (fail-CLOSED for anything that trades): confirm persistence, or — only when
we're clearly NOT on Railway (local dev / CI, where the DB shares the root device)
— allow. On Railway without a confirmed volume, refuse.
"""

from __future__ import annotations

import logging
import os
from typing import Any

# Any of these present => we're running on Railway (they're part of the service
# env and are injected on the cutover container too, which is exactly when the
# volume may be missing).
_RAILWAY_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_REPLICA_ID",
    "RAILWAY_VOLUME_MOUNT_PATH",
    "RAILWAY_VOLUME_NAME",
)


def on_railway() -> bool:
    return any(os.environ.get(k) for k in _RAILWAY_MARKERS)


def sqlite_dir(db_url: str) -> str | None:
    """The directory holding the SQLite file for a ``sqlite://`` URL, or None for
    a non-sqlite backend (Postgres etc. — persistence isn't filesystem-bound)."""
    if not db_url.startswith("sqlite:"):
        return None
    # sqlite:///relative/x.db -> relative/x.db ; sqlite:////abs/x.db -> /abs/x.db
    prefix = "sqlite:///"
    path = db_url[len(prefix):] if db_url.startswith(prefix) else db_url[len("sqlite:"):]
    if path in ("", ":memory:") or path.startswith(":memory:"):
        return None  # in-memory DBs are non-persistent by nature (tests) — not guarded
    return os.path.dirname(os.path.abspath(path)) or os.path.abspath(path)


def is_persistent_mount(path: str) -> bool:
    """True if ``path`` lives on a different device than the container root '/',
    i.e. a real mounted volume rather than the overlay filesystem. Robust to
    subpaths of the mount; False on any stat error (fail-closed)."""
    try:
        return os.stat(path).st_dev != os.stat("/").st_dev
    except OSError:
        return False


def volume_status(db_url: str | None = None) -> dict[str, Any]:
    """Assess whether the DB is on a persistent volume. ``ok`` is the trade/write
    gate; ``persistent`` is the raw filesystem verdict."""
    if db_url is None:
        from pipeline.common.db import database_url

        db_url = database_url()
    railway = on_railway()
    dbdir = sqlite_dir(db_url)
    if dbdir is None:
        # non-sqlite (or in-memory): filesystem-mount reasoning doesn't apply.
        return {
            "ok": True,
            "on_railway": railway,
            "db_dir": None,
            "persistent": True,
            "railway_volume_path": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
            "reason": "non-sqlite/in-memory backend (not filesystem-bound)",
        }
    persistent = is_persistent_mount(dbdir)
    ok = persistent or not railway
    if persistent:
        reason = f"persistent volume confirmed at {dbdir} (separate device from /)"
    elif not railway:
        reason = f"not on Railway; {dbdir} shares the root device (local/CI — allowed)"
    else:
        reason = (
            f"EPHEMERAL: {dbdir} is on the container overlay, not a mounted volume — "
            "writes here will NOT persist"
        )
    return {
        "ok": ok,
        "on_railway": railway,
        "db_dir": dbdir,
        "persistent": persistent,
        "railway_volume_path": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
        "reason": reason,
    }


def require_persistent_volume(log: logging.Logger, purpose: str, db_url: str | None = None) -> bool:
    """Gate a trade/write path on volume persistence. Logs a loud, self-explaining
    banner and returns True (proceed) / False (refuse). Fail-closed on Railway."""
    st = volume_status(db_url)
    if st["ok"]:
        if st["on_railway"]:
            log.info("VOLUME OK (%s): %s", purpose, st["reason"])
        return True
    log.error(
        "=== EPHEMERAL CONTAINER — NO PERSISTENT VOLUME === %s REFUSED. %s "
        "(RAILWAY_VOLUME_MOUNT_PATH=%r). This is almost certainly a deploy cutover; "
        "the real container will run on the mounted volume.",
        purpose, st["reason"], st["railway_volume_path"],
    )
    return False
