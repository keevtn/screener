"""Initialize the pipeline database: create all tables + SQLite append-only triggers.

Usage:
    python scripts/init_db.py [--url sqlite:///data/pipeline.db] [--echo]

Idempotent: create_all skips existing tables.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import sqlalchemy as sa

from pipeline.common.db import database_url, make_engine
from pipeline.common.models import Base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    parser.add_argument("--echo", action="store_true", help="log emitted SQL")
    args = parser.parse_args()

    url = args.url or database_url()
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = make_engine(url, echo=args.echo)
    Base.metadata.create_all(engine)

    tables = sa.inspect(engine).get_table_names()
    print(f"initialized {url}")
    print(f"tables: {', '.join(sorted(tables))}")


if __name__ == "__main__":
    main()
