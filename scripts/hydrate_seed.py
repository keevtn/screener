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
            for t in seed_tables:
                if t not in have:
                    skipped.append(t)  # live schema predates this seed table — skip safely
                    continue
                before = con.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                if before > 0 and not args.force:
                    skipped.append(t)  # live history present — don't clobber
                    continue
                con.execute(f'INSERT OR IGNORE INTO main."{t}" SELECT * FROM seed."{t}"')
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
        if violations:
            print(f"[hydrate] WARNING: {len(violations)} foreign-key violations: {violations[:5]}",
                  file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
