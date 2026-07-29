"""Export a SLIM seed DB for porting demo history to a fresh (Railway) volume.

The live pipeline.db is dominated by two families that Railway does NOT need
shipped — they re-accumulate on their own from the live feed, and Railway trial
volumes are small (~0.5 GB):

  * raw_items                — the append-only news archive (the bulk)
  * clusters / cluster_scores / cluster_entities / unmapped_mentions /
    signal_observations / armed_states
                             — everything FK-chained to raw_items (PRAGMA
                               foreign_keys=ON means they can't be shipped
                               without raw_items anyway)

What the seed KEEPS is the demo-worthy history that is self-contained once
`configs` rides along: the prediction ledger + graded outcomes (`predictions`),
the attention chart history (`attention_daily`, `buzz_baselines`,
`search_interest_daily`), the universe (`entities`, `fundamentals_snapshots`,
`scheduled_events`, `universe_snapshots`), the paper-trading report cards
(`sim_configs`, `sim_trades`, `sim_daily_summary`, `premarket_panels`,
`extended_session_daily`), and the AI proposal artifacts (`ranking_runs`,
`rankings`, `ticker_analyses`, `llm_spend`, `pending_changes`).

The seed is a plain SQLite file with the whitelisted TABLES only (no secondary
indexes — the live DB builds its own on init, and dropping them keeps the seed
small). Hydrate it with scripts/hydrate_seed.py.

Usage:
    python scripts/export_seed.py --source path/to/pipeline.db --out seed/pipeline_seed.db
    # --source defaults to $DATABASE_URL's sqlite path, else data/pipeline.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

# Excluded from the seed: the append-only archive + everything FK-chained to it.
# Any table NOT in this set is shipped. (armed_states is FK->clusters and empty;
# excluded to keep the "nothing referencing the cluster family" rule clean.)
EXCLUDE = {
    "raw_items",
    "clusters",
    "cluster_scores",
    "cluster_entities",
    "unmapped_mentions",
    "signal_observations",
    "armed_states",
    "sqlite_sequence",
}


def _default_source() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///data/pipeline.db")
    return url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else "data/pipeline.db"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="full pipeline.db to export from")
    ap.add_argument("--out", default="seed/pipeline_seed.db", help="slim seed output path")
    args = ap.parse_args()

    source = Path(args.source or _default_source()).resolve()
    out = Path(args.out).resolve()
    if not source.exists():
        raise SystemExit(f"source DB not found: {source}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    # Discover shippable tables + their exact CREATE TABLE DDL from the source.
    tables = [
        (name, sql)
        for name, sql in src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if name not in EXCLUDE
    ]
    src.close()

    dst = sqlite3.connect(out)
    dst.execute("PRAGMA foreign_keys=OFF")  # FK order irrelevant during a bulk subset copy
    dst.execute(f"ATTACH DATABASE '{source.as_posix()}' AS src")
    counts: dict[str, int] = {}
    with dst:  # single transaction
        for name, ddl in tables:
            dst.execute(ddl)  # tables only — no secondary indexes (keeps the seed small)
            dst.execute(f'INSERT INTO main."{name}" SELECT * FROM src."{name}"')
            counts[name] = dst.execute(f'SELECT COUNT(*) FROM main."{name}"').fetchone()[0]
    dst.execute("DETACH DATABASE src")
    dst.execute("VACUUM")
    dst.close()

    total = sum(counts.values())
    print(f"seed written: {out}")
    print(f"  {len(tables)} tables, {total:,} rows")
    for name in sorted(counts, key=lambda k: -counts[k]):
        if counts[name]:
            print(f"    {name:<28} {counts[name]:>10,}")
    print(f"  source: {source.stat().st_size / 1024 / 1024:6.1f} MB")
    print(f"  seed:   {out.stat().st_size / 1024 / 1024:6.1f} MB")


if __name__ == "__main__":
    main()
