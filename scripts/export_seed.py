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

The seed also ships `prediction_context` — the per-prediction origin-news context
(source_class / headline / url) the LEDGER lanes need. It is NOT a copyable table
(the source's cluster family that it derives from is excluded), so it is COMPUTED
during export from the read-only source join and written into the seed directly.

The seed is a plain SQLite file with the whitelisted TABLES only (no secondary
indexes — the live DB builds its own on init, and dropping them keeps the seed
small). Hydrate it with scripts/hydrate_seed.py.

Usage:
    python scripts/export_seed.py --source path/to/pipeline.db --out seed/pipeline_seed.db
    # --source defaults to $DATABASE_URL's sqlite path, else data/pipeline.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
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


# prediction_context DDL. Column ORDER must mirror models.PredictionContext so the
# hydrate step's `INSERT ... SELECT *` lines up with the live (init_db) schema. The
# CHECK/FK constraints are harmless in the seed (hydrate runs foreign_keys=OFF).
# KEEP IN SYNC with pipeline.common.models.PredictionContext.
_PREDICTION_CONTEXT_DDL = """
CREATE TABLE prediction_context (
    prediction_id VARCHAR NOT NULL,
    source_class VARCHAR(12),
    headline TEXT,
    url TEXT,
    source VARCHAR(200),
    cluster_id VARCHAR(64),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (prediction_id),
    CONSTRAINT ck_prediction_context_source_class
        CHECK (source_class IS NULL OR source_class IN ('structured', 'social', 'mixed')),
    FOREIGN KEY(prediction_id) REFERENCES predictions (prediction_id)
)
"""

# SQLAlchemy's sqlite DATETIME storage format (space-separated, tz stripped) — write
# created_at this way so the live app's UTCDateTime reader parses it back.
_SA_DT = "%Y-%m-%d %H:%M:%S.%f"


def _classify(classes: set[str]) -> str | None:
    if not classes:
        return None
    if classes == {"structured"}:
        return "structured"
    if classes == {"social"}:
        return "social"
    return "mixed"  # contributing origins disagree (rare; kept explicit)


def _build_prediction_context(dst: sqlite3.Connection) -> int:
    """Compute the companion prediction_context table (LEDGER lanes) into the seed.

    The full source (attached as ``src``, read only here — we never write to it)
    carries the cluster/raw_items family the slim seed omits, so each prediction's
    origin-news context (source_class, headline, url, source) is RESOLVED from that
    join and shipped in the seed — the hydrated Railway volume never sees the
    cluster family. Resolution mirrors the live arm-time resolver
    (pipeline.common.prediction_context); KEEP THE TWO IN SYNC.
    """
    dst.execute(_PREDICTION_CONTEXT_DDL)

    # cluster_id -> origin raw_item, then origin -> (source_class, url, source, title).
    origin_of = dict(dst.execute("SELECT cluster_id, origin_item_id FROM src.clusters").fetchall())
    raw: dict[str, tuple] = {}
    for rid, sc, url, srcname, payload in dst.execute(
        "SELECT id, source_class, url, source, payload_json FROM src.raw_items"
    ).fetchall():
        title = None
        if payload:
            try:
                title = json.loads(payload).get("title")
            except (ValueError, TypeError, AttributeError):
                title = None
        raw[rid] = (sc, url, srcname, title)

    def ctx_from_clusters(cids: list[str]) -> dict | None:
        resolvable = [(c, raw[origin_of[c]]) for c in cids if origin_of.get(c) in raw]
        if not resolvable:
            return None
        classes = {info[0] for _, info in resolvable if info[0] is not None}
        primary_cid, (sc, url, srcname, title) = resolvable[0]  # first in cited order
        return {
            "source_class": _classify(classes),
            "headline": title,
            "url": url,
            "source": srcname,
            "cluster_id": primary_cid,
        }

    resolved: dict[str, dict] = {}  # real pred_id -> ctx (for baseline shadow inherit)
    shadows: list[tuple[str, str]] = []  # (baseline pred_id, shadowed real pred_id)
    rows: list[dict] = []
    for pid, ej in dst.execute("SELECT prediction_id, evidence_json FROM src.predictions").fetchall():
        ev = {}
        if ej:
            try:
                ev = json.loads(ej)
            except (ValueError, TypeError):
                ev = {}
        cids = ev.get("cluster_ids")
        if not (isinstance(cids, list) and cids):
            armed = ev.get("armed_cluster_id")
            cids = [armed] if isinstance(armed, str) and armed else []
        if cids:
            ctx = ctx_from_clusters([c for c in cids if isinstance(c, str)])
            if ctx is not None:
                resolved[pid] = ctx
                rows.append({"prediction_id": pid, **ctx})
        else:
            shadow = ev.get("shadows")
            if isinstance(shadow, str) and shadow:
                shadows.append((pid, shadow))

    for pid, shadow in shadows:  # baselines inherit the shadowed pred's origin
        ctx = resolved.get(shadow)
        if ctx is not None:
            rows.append({"prediction_id": pid, **ctx})

    now = datetime.now(UTC).strftime(_SA_DT)
    cols = ["prediction_id", "source_class", "headline", "url", "source", "cluster_id", "created_at"]
    dst.executemany(
        f'INSERT INTO main."prediction_context" ({",".join(cols)}) '
        f"VALUES ({','.join('?' for _ in cols)})",
        [tuple(now if c == "created_at" else r.get(c) for c in cols) for r in rows],
    )
    return len(rows)


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
        # Companion origin-news context, COMPUTED from the source's cluster family
        # (attached as src, read-only here) — not a copyable table since the seed
        # omits clusters.
        counts["prediction_context"] = _build_prediction_context(dst)
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
