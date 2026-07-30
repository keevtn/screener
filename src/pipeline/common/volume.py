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
    subpaths of the mount; False on any stat error (fail-closed).

    NOTE: this is a SECONDARY signal. Observed in production, a real Railway
    volume does NOT always present a distinct st_dev (the mount can share the
    root device), so a False here does not by itself mean 'ephemeral' — see
    mount_path_confirms for the primary Railway proof."""
    try:
        return os.stat(path).st_dev != os.stat("/").st_dev
    except OSError:
        return False


def mount_path_confirms(db_dir: str) -> bool:
    """PRIMARY Railway proof: RAILWAY_VOLUME_MOUNT_PATH is set, the DB directory
    resolves UNDER that mount path, and the mount is writable.

    This is trustworthy because Railway injects RAILWAY_VOLUME_MOUNT_PATH only for
    a container that actually has the volume attached — a true deploy-cutover
    container running volume-less does not get it (or the DB won't resolve under
    it), so this stays False there and the trade path still refuses."""
    mp = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not mp:
        return False
    try:
        db = os.path.realpath(db_dir)
        mount = os.path.realpath(mp)
    except OSError:
        return False
    under = db == mount or db.startswith(mount.rstrip("/\\") + os.sep)
    return under and os.access(mp, os.W_OK)


def _sqlite_file(db_url: str) -> str | None:
    """Absolute path to the SQLite file for a ``sqlite://`` URL, else None."""
    if not db_url.startswith("sqlite:"):
        return None
    prefix = "sqlite:///"
    path = db_url[len(prefix):] if db_url.startswith(prefix) else db_url[len("sqlite:"):]
    if path in ("", ":memory:") or path.startswith(":memory:"):
        return None
    return os.path.abspath(path)


# The slim seed deliberately EXCLUDES the bulk news archive, so raw_items /
# clusters aren't shipped in it — a freshly-hydrated (ephemeral) DB has them
# EMPTY, while a persistent volume accumulates thousands. This many rows in one of
# them is therefore positive, env-independent proof of live accumulation on THIS
# database = the persistent volume. (Kept well above any handful an ephemeral
# container might ingest during a brief cutover boot, and well below the
# hundreds/thousands a real volume carries.)
_SEED_EXCLUDED_TABLES = ("raw_items", "clusters")
_ACCUMULATION_THRESHOLD = 200


def data_beyond_seed(db_url: str, threshold: int = _ACCUMULATION_THRESHOLD) -> bool:
    """True if the live DB carries live-accumulated data the shipped seed never
    contained (a non-trivial raw_items/clusters count). Read-only; fail-soft to
    False (then other proofs decide)."""
    import sqlite3

    live = _sqlite_file(db_url)
    if not live or not os.path.exists(live):
        return False
    try:
        con = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        best = 0
        for t in _SEED_EXCLUDED_TABLES:
            try:
                best = max(best, con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            except sqlite3.Error:
                continue  # table absent on an older schema — skip
        return best >= threshold
    finally:
        con.close()


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
            "confirmed": True,
            "beyond_seed": False,
            "mount_confirmed": False,
            "persistent": True,
            "railway_volume_path": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
            "reason": "non-sqlite/in-memory backend (not filesystem-bound)",
        }
    # Three independent positive proofs; ANY confirms persistence:
    #   1. data_beyond_seed — the DB carries live-accumulated rows the slim seed
    #      never shipped (raw_items/clusters). Env- and filesystem-independent, and
    #      the most reliable in practice: a just-hydrated ephemeral DB has none.
    #   2. mount_path_confirms — RAILWAY_VOLUME_MOUNT_PATH set + DB under it + writable.
    #   3. is_persistent_mount — DB on a different device than '/'.
    beyond_seed = data_beyond_seed(db_url)
    mount_ok = mount_path_confirms(dbdir)
    persistent = is_persistent_mount(dbdir)
    confirmed = beyond_seed or mount_ok or persistent
    ok = confirmed or not railway
    if beyond_seed:
        reason = f"volume confirmed: {dbdir} has live-accumulated data beyond the seed"
    elif mount_ok:
        reason = f"volume confirmed: {dbdir} under RAILWAY_VOLUME_MOUNT_PATH (writable)"
    elif persistent:
        reason = f"volume confirmed: {dbdir} on a separate device from /"
    elif not railway:
        reason = f"not on Railway; {dbdir} shares the root device (local/CI — allowed)"
    else:
        reason = (
            f"EPHEMERAL: {dbdir} has no data beyond the seed, no RAILWAY_VOLUME_MOUNT_PATH "
            "match, and shares the device with / — writes here will NOT persist"
        )
    return {
        "ok": ok,
        "on_railway": railway,
        "db_dir": dbdir,
        "confirmed": confirmed,
        "beyond_seed": beyond_seed,
        "mount_confirmed": mount_ok,
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
