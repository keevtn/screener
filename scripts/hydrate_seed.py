"""Hydrate a fresh (Railway) DB from the slim seed — the port path for demo history.

Boot-time contract (see scripts/railway_start.sh): a fresh Railway volume starts
EMPTY. This copies the shipped demo history (prediction ledger, graded outcomes,
attention chart, universe, paper-trading report cards, AI proposals — everything
in seed/pipeline_seed.db) into the live DB, ONCE, and never on top of real data.

Idempotency: seeding is gated PER TABLE on emptiness — a table is seeded only when
the live one is empty, so a restart never double-seeds and never clobbers history
the running pipeline has since accumulated. This also means a table added AFTER the
volume was first hydrated (e.g. prediction_context, the LEDGER-lane companion) is
still seeded on the next boot instead of staying forever empty behind an
already-populated ledger. Pass --force to seed every table regardless (INSERT OR
IGNORE, so existing PKs are preserved).

Seed source: the committed seed/pipeline_seed.db by default. If it is absent and
$SEED_DB_URL (or --seed-url) is set, the seed is downloaded there first (httpx,
already a runtime dep) with an optional $SEED_DB_SHA256 integrity check — the
lean-repo alternative to committing the 28 MB file.

Order matters only in that the schema (tables + append-only triggers) must exist
first; railway_start.sh runs init_db.py immediately before this, and we also
ensure it here so the script is safe to run standalone. The seed excludes
raw_items and the whole cluster family, so nothing it inserts can trip the
raw_items append-only triggers or a dangling FK.

Usage:
    python scripts/hydrate_seed.py                       # if-empty, from committed seed
    python scripts/hydrate_seed.py --force               # seed even if non-empty
    DATABASE_URL=sqlite:///data/pipeline.db python scripts/hydrate_seed.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def _live_sqlite_path(url: str) -> str:
    if not url.startswith("sqlite:///"):
        raise SystemExit(f"hydrate_seed only supports sqlite:/// URLs, got: {url}")
    return url.removeprefix("sqlite:///")


def _ensure_schema(url: str) -> None:
    """Create tables + append-only triggers on the live DB if missing (idempotent)."""
    try:
        from pipeline.common.db import make_engine
        from pipeline.common.models import Base
    except Exception as exc:  # pragma: no cover - only if PYTHONPATH is wrong
        print(f"[hydrate] could not import pipeline to ensure schema ({exc}); "
              "assuming init_db.py already ran", file=sys.stderr)
        return
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()


def _download(url: str, dest: Path, sha256: str | None) -> None:
    import httpx

    print(f"[hydrate] downloading seed from {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
                h.update(chunk)
    if sha256 and h.hexdigest() != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[hydrate] seed checksum mismatch: got {h.hexdigest()}, want {sha256}")
    tmp.replace(dest)


_REPAIR_TABLE = "fundamentals_snapshots"
# The corruption signature: a datetime-shaped string physically sitting in the
# numeric `price` column. Healthy price is always REAL or NULL, so a TEXT value
# starting YYYY-MM-DD can ONLY be a created_at timestamp shifted in by an old
# positional `SELECT *` copy. GLOB (not LIKE) is case/locale-free and index-free.
_SHIFT_SIGNATURE = (
    "typeof(price) = 'text' AND price GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*'"
)


def _repair_shifted_fundamentals(con: sqlite3.Connection) -> int:
    """Self-heal the ONE known-corruptable snapshot table on boot.

    ``fundamentals_snapshots`` is rebuildable snapshot data (not an append-only
    ledger), so if the live volume shows the column-shift signature (a timestamp
    string in the numeric ``price`` column), we wipe JUST that table's rows and let
    the per-table emptiness gate re-copy it from the seed via the name-based path.
    Returns the number of corrupt rows detected (0 = nothing repaired).

    Guarded tightly: touches no other table; wipes only when the specific signature
    is present AND the seed actually has rows to restore from — so it can never
    trigger on healthy data or empty the table with nothing to refill it."""
    live = con.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (_REPAIR_TABLE,)
    ).fetchone()
    seed = con.execute(
        "SELECT 1 FROM seed.sqlite_master WHERE type='table' AND name=?", (_REPAIR_TABLE,)
    ).fetchone()
    if not live or not seed:
        return 0
    corrupt = con.execute(
        f'SELECT COUNT(*) FROM main."{_REPAIR_TABLE}" WHERE {_SHIFT_SIGNATURE}'
    ).fetchone()[0]
    if corrupt == 0:
        return 0
    seed_rows = con.execute(f'SELECT COUNT(*) FROM seed."{_REPAIR_TABLE}"').fetchone()[0]
    if seed_rows == 0:
        print(
            f"[hydrate] WARNING: {corrupt:,} shifted {_REPAIR_TABLE} rows detected but the "
            "seed has none to restore from — leaving the table as-is (backend coercion "
            "still neutralizes the bad values).",
            file=sys.stderr,
        )
        return 0
    total = con.execute(f'SELECT COUNT(*) FROM main."{_REPAIR_TABLE}"').fetchone()[0]
    print(
        f"[hydrate] REPAIR: detected {corrupt:,}/{total:,} {_REPAIR_TABLE} rows with a "
        f"timestamp shifted into the numeric price column — wiping this table ONLY and "
        f"re-copying {seed_rows:,} clean rows from the seed by column name."
    )
    con.execute(f'DELETE FROM main."{_REPAIR_TABLE}"')
    return corrupt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", "sqlite:///data/pipeline.db"))
    ap.add_argument("--seed", default=os.environ.get("SEED_DB_PATH", "seed/pipeline_seed.db"))
    ap.add_argument("--seed-url", default=os.environ.get("SEED_DB_URL"))
    ap.add_argument("--force", action="store_true", help="seed even if the ledger is non-empty")
    args = ap.parse_args()

    live_path = _live_sqlite_path(args.url)
    Path(live_path).parent.mkdir(parents=True, exist_ok=True)
    _ensure_schema(args.url)

    seed = Path(args.seed)
    if not seed.exists():
        if args.seed_url:
            _download(args.seed_url, seed, os.environ.get("SEED_DB_SHA256"))
        else:
            print(f"[hydrate] no seed at {seed} and no SEED_DB_URL set; nothing to hydrate")
            return

    con = sqlite3.connect(live_path)
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        have = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "predictions" in have and not args.force:
            n = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            if n > 0:
                print(f"[hydrate] live ledger already has {n:,} predictions; seeding only "
                      "still-empty tables (use --force to seed all)")

        con.execute(f"ATTACH DATABASE '{seed.resolve().as_posix()}' AS seed")
        seed_tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM seed.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        # PER-TABLE emptiness gate (replaces the old all-or-nothing skip): seed a
        # table only when the live one is EMPTY, so a restart never clobbers history
        # the running pipeline has accumulated — but a table added AFTER the volume
        # was first hydrated (e.g. prediction_context, the LEDGER-lane companion)
        # still receives its backfilled rows instead of staying forever empty.
        # --force seeds every table regardless (INSERT OR IGNORE preserves live PKs).
        inserted: dict[str, int] = {}
        skipped: list[str] = []
        with con:
            # Self-heal already-shifted fundamentals rows first: wiping the table
            # here drops its row count to 0, so the emptiness gate below re-copies
            # it (by column name) from the seed with correct values.
            repaired = _repair_shifted_fundamentals(con)
            for t in seed_tables:
                if t not in have:
                    skipped.append(t)  # live schema predates this seed table — skip safely
                    continue
                before = con.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                if before > 0 and not args.force:
                    skipped.append(t)  # live history present — don't clobber
                    continue
                # Copy by COLUMN NAME, not `SELECT *`. The seed inherits the SOURCE
                # DB's physical column order, which can differ from the live schema
                # created by init_db (e.g. fundamentals_snapshots had price/change_pct
                # ALTER-appended after created_at on the source, but the model defines
                # them before it). A positional `SELECT *` then shifts values into the
                # wrong columns — that's how a created_at TIMESTAMP once landed in the
                # numeric `price` column and crashed the UNIVERSE panel. Intersecting on
                # name in the live table's order makes the copy order-independent.
                main_cols = [r[1] for r in con.execute(f'PRAGMA main.table_info("{t}")').fetchall()]
                seed_cols = {r[1] for r in con.execute(f'PRAGMA seed.table_info("{t}")').fetchall()}
                cols = [c for c in main_cols if c in seed_cols]
                collist = ", ".join(f'"{c}"' for c in cols)
                con.execute(
                    f'INSERT OR IGNORE INTO main."{t}" ({collist}) '
                    f'SELECT {collist} FROM seed."{t}"'
                )
                after = con.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                inserted[t] = after - before
        con.execute("DETACH DATABASE seed")

        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        total = sum(inserted.values())
        filled = {t: n for t, n in inserted.items() if n}
        print(
            f"[hydrate] inserted {total:,} rows across {len(filled)} tables from {seed} "
            f"({len(skipped)} already-populated/absent tables skipped)"
        )
        for t in sorted(filled, key=lambda k: -filled[k]):
            print(f"[hydrate]   {t:<28} +{filled[t]:,}")
        if repaired:
            restored = inserted.get(_REPAIR_TABLE, 0)
            print(
                f"[hydrate] REPAIR complete: replaced {repaired:,} shifted {_REPAIR_TABLE} "
                f"rows with {restored:,} clean rows re-copied from the seed."
            )
        if violations:
            print(f"[hydrate] WARNING: {len(violations)} foreign-key violations: {violations[:5]}",
                  file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
