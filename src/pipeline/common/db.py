"""Engine/session factories. DATABASE_URL env selects the backend;
SQLite now, Postgres later with no call-site changes (docs/ROADMAP.md task 0.2)."""

from __future__ import annotations

import os
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///data/pipeline.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(
    url: str | None = None, *, echo: bool = False, busy_timeout_ms: int = 5000
) -> Engine:
    engine = sa.create_engine(url or database_url(), echo=echo)
    if engine.dialect.name == "sqlite":

        @sa.event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL: concurrent readers during a write, fewer lock stalls (task 0.2, rev 2).
            # Persisted in the DB file header, but set on every connect so a fresh
            # file adopts it immediately. ROADMAP-NOTE: WAL needs an on-disk DB; an
            # in-memory URL silently stays in "memory" journal mode.
            cursor.execute("PRAGMA journal_mode=WAL")
            # busy_timeout: a second WRITER (WAL still serializes writes) waits up
            # to this long for the lock instead of raising "database is locked".
            # The default 5s suffices for serve_api + data-prep jobs, but a writer
            # sharing pipeline.db with the busy pipeline loop (e.g. the paper-sim
            # driver) needs longer — a heavy sweep can hold the write lock past 5s,
            # and a raised lock there orphaned a filled order on 2026-07-17.
            # Per-connection, so set on every connect.
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


# Performance indexes that a long-lived DB predates. create_all() adds them for
# fresh DBs from the model declarations; this backfills them onto an existing DB.
# Names match SQLAlchemy's own (ix_<table>_<col>) so create_all + this converge.
_PERF_INDEXES = (
    ("ix_raw_items_ingested_at", "raw_items", "ingested_at"),
    # /health does WHERE source_class=? MAX(ingested_at) — needs the composite to
    # serve filter+aggregate together (the single-column index can't).
    ("ix_raw_items_source_class_ingested", "raw_items", "source_class, ingested_at"),
    ("ix_predictions_status", "predictions", "status"),
    ("ix_fundamentals_snapshots_as_of", "fundamentals_snapshots", "as_of"),
    ("ix_cluster_entities_ticker_created", "cluster_entities", "ticker, created_at"),
)


def ensure_indexes(engine: Engine) -> list[str]:
    """Idempotently create the performance indexes on an existing DB (CREATE
    INDEX IF NOT EXISTS). No-op where create_all already made them. SQLite only;
    returns the list of index names ensured."""
    if engine.dialect.name != "sqlite":
        return []
    import sqlalchemy as _sa

    made = []
    with engine.begin() as conn:
        for name, table, cols in _PERF_INDEXES:
            conn.execute(_sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})"))
            made.append(name)
    return made
