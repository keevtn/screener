"""Task 0.2 (rev 2) gate test: a fresh SQLite connection reports journal_mode=wal."""

from __future__ import annotations

import sqlalchemy as sa

from pipeline.common.db import make_engine


def test_wal_enabled(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    try:
        with engine.connect() as conn:
            mode = conn.execute(sa.text("PRAGMA journal_mode")).scalar_one()
        assert mode.lower() == "wal"
    finally:
        engine.dispose()


def test_busy_timeout_set(tmp_path):
    # Second writer waits for the lock instead of raising "database is locked"
    # immediately (prevents serve_api ↔ data-prep collisions crashing a window).
    engine = make_engine(f"sqlite:///{tmp_path / 'bt.db'}")
    try:
        with engine.connect() as conn:
            timeout = conn.execute(sa.text("PRAGMA busy_timeout")).scalar_one()
        assert timeout == 5000
    finally:
        engine.dispose()
