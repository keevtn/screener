"""Idempotent schema migration (docs/ROADMAP.md — bridges the create_all gap).

`init_db.py` (create_all) adds missing TABLES but never ALTERs existing ones, so a
DB created under an older model is missing newer columns. This adds both: missing
tables (create_all) and missing columns (ALTER TABLE ADD COLUMN, nullable). No
Alembic; safe to re-run.

    python scripts/migrate.py [--url DATABASE_URL]
"""

from __future__ import annotations

import argparse

from sqlalchemy import inspect, text

from pipeline.common.db import make_engine
from pipeline.common.models import Base


def migrate(url: str | None = None) -> list[str]:
    engine = make_engine(url)
    Base.metadata.create_all(engine)  # add any missing tables
    insp = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                # Added columns are nullable (SQLite can't add NOT NULL w/o default).
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"))
                added.append(f"{table.name}.{col.name}")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    args = parser.parse_args()
    added = migrate(args.url)
    if added:
        print(f"migrated: added {len(added)} column(s): {', '.join(added)}")
    else:
        print("schema up to date (tables ensured; no missing columns)")


if __name__ == "__main__":
    main()
