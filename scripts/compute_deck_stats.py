#!/usr/bin/env python
"""Regenerate the deck's headline numbers live from data + repo. STRICTLY READ-ONLY.

Every figure the submission deck cites should be transparently derivable. This
script recomputes them from (a) the pipeline DB and (b) the repo itself, and prints
the CURRENT numbers honestly -- if a figure has drifted from what the deck shows
(latency varies by load; cohorts grow as more predictions grade), the live number
is printed as-is, not massaged to match.

    python scripts/compute_deck_stats.py
    python scripts/compute_deck_stats.py --source ../Financial-News-Screener/data/pipeline.db

The slim shipped seed excludes raw_items / cluster_scores / signal_observations, so
latency + observation-cohort stats need the FULL local DB via --source; each stat
degrades to "n/a (table absent)" rather than failing when its inputs are missing.

Definitions + canonical sources for every number: see docs/results_methods.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The deck's cited figures, for a side-by-side drift check (annotate, never fudge).
DECK = {
    "latency_median_s": 38,
    "hit_rate_pct": 47,
    "sim_net_per_trade_pct": -1.0,
    "highalert_expansion_pct": 30,
    "a11y_before": 462,
    "a11y_after": 0,
    "tests": 500,
    "endpoints": 67,
    "tables": 29,
    "loc_k": 36,
    "items_per_day_k": 10,
}


# --------------------------------------------------------------------------- #
# DB helpers (read-only)
# --------------------------------------------------------------------------- #
def resolve_db_path(source: str | None) -> str:
    if source:
        return source
    url = os.environ.get("DATABASE_URL", f"sqlite:///{REPO / 'data' / 'pipeline.db'}")
    if url.startswith("sqlite"):
        return url.split("///", 1)[-1]
    return url


def connect_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_table(cur: sqlite3.Cursor, name: str) -> bool:
    return (
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def baseline_sets(cur: sqlite3.Cursor) -> tuple[set[str], set[str]]:
    """(real, baseline) config_version sets. A config is a baseline iff its params
    carry a 'baseline' key (grade.baselines.ensure_baseline_configs)."""
    real: set[str] = set()
    base: set[str] = set()
    for row in cur.execute("SELECT config_version, params_json FROM configs"):
        try:
            p = json.loads(row["params_json"]) if isinstance(row["params_json"], str) else (row["params_json"] or {})
        except (ValueError, TypeError):
            p = {}
        (base if isinstance(p, dict) and p.get("baseline") else real).add(row["config_version"])
    return real, base


def _median_p90(xs: list[float]) -> tuple[float, float]:
    xs = sorted(xs)
    med = statistics.median(xs)
    p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
    return med, p90


def _in(s: set[str]) -> str:
    return "(" + ",".join("?" * len(s)) + ")"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def section_ledger(cur: sqlite3.Cursor) -> None:
    print("LEDGER (predictions + origin context)")
    if not has_table(cur, "predictions"):
        print("  n/a (predictions table absent)")
        return
    real, base = baseline_sets(cur)
    total = cur.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    graded = cur.execute("SELECT COUNT(*) FROM predictions WHERE status='graded'").fetchone()[0]
    real_n = (
        cur.execute(f"SELECT COUNT(*) FROM predictions WHERE config_version IN {_in(real)}", list(real)).fetchone()[0]
        if real else 0
    )
    base_n = total - real_n
    if has_table(cur, "prediction_context"):
        ctx_any = cur.execute("SELECT COUNT(*) FROM prediction_context").fetchone()[0]
        ctx_res = cur.execute(
            "SELECT COUNT(*) FROM prediction_context WHERE source_class IS NOT NULL"
        ).fetchone()[0]
        pct_ctx = f"{100 * ctx_any / total:.1f}%" if total else "n/a"
        pct_res = f"{100 * ctx_res / total:.1f}%" if total else "n/a"
    else:
        pct_ctx = pct_res = "n/a (prediction_context absent)"
    print(f"  total predictions      {total:,}")
    print(f"  real (non-baseline)    {real_n:,}   baseline shadows {base_n:,}")
    print(f"  graded                 {graded:,}")
    print(f"  with origin context    {pct_ctx}  (resolved source_class {pct_res})")


def section_throughput(cur: sqlite3.Cursor) -> None:
    print("\nINGEST THROUGHPUT (raw_items volume)")
    if not has_table(cur, "raw_items"):
        print("  n/a (raw_items excluded from the slim seed; use --source)")
        return
    total = cur.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    last24 = cur.execute(
        "SELECT COUNT(*) FROM raw_items WHERE ingested_at >= datetime('now','-1 day')"
    ).fetchone()[0]
    per_day7 = cur.execute(
        "SELECT COUNT(*) FROM raw_items WHERE ingested_at >= datetime('now','-7 day')"
    ).fetchone()[0] / 7.0
    print(f"  total raw_items        {total:,}")
    print(f"  last 24h               {last24:,}")
    print(f"  mean/day (last 7d)     {per_day7:,.0f}   (deck ~{DECK['items_per_day_k']}k/day)")


def section_latency(cur: sqlite3.Cursor, window: int) -> None:
    print("\nINGEST -> SCORED LATENCY (raw_items.ingested_at -> cluster_scores.created_at)")
    if not (has_table(cur, "cluster_scores") and has_table(cur, "raw_items") and has_table(cur, "clusters")):
        print("  n/a (needs raw_items + clusters + cluster_scores -- excluded from the slim seed; use --source)")
        return
    rows = cur.execute(
        """SELECT (julianday(cs.created_at) - julianday(ri.ingested_at)) * 86400.0 AS d
           FROM cluster_scores cs
           JOIN clusters cl ON cl.cluster_id = cs.cluster_id
           JOIN raw_items ri ON ri.id = cl.origin_item_id
           WHERE cs.created_at IS NOT NULL AND ri.ingested_at IS NOT NULL
           ORDER BY cs.created_at DESC LIMIT ?""",
        (window,),
    ).fetchall()
    deltas = [r["d"] for r in rows if r["d"] is not None]
    if not deltas:
        print("  n/a (no joinable ingest/score pairs)")
        return
    # Live path = scored promptly after ingest. Backfilled/re-scored rows carry huge
    # deltas (item ingested long before this scoring pass) and are excluded from the
    # live-latency figure; the unfiltered median is printed too, for honesty.
    live = [d for d in deltas if 0 <= d < 3600]
    med_all = statistics.median(deltas)
    print(f"  window                 last {len(deltas):,} scored clusters")
    if live:
        med, p90 = _median_p90(live)
        print(f"  live (0-3600s)  n={len(live):,}   median {med:.1f}s   p90 {p90:.1f}s")
    print(f"  unfiltered median      {med_all:.1f}s")
    print(f"  deck-cited (38s era)   vs current median {statistics.median(live) if live else med_all:.1f}s "
          f"-- latency scales with scorer load / SENTIMENT_MODE; annotate, don't fudge")


def section_cohorts(cur: sqlite3.Cursor) -> None:
    print("\nPER-GATE COHORTS (recomputable from stored graded data)")
    if not has_table(cur, "predictions"):
        print("  n/a (predictions table absent)")
    else:
        real, base = baseline_sets(cur)
        # Sentiment-direction cohort = REAL graded predictions (adjusted return is the
        # benchmark-relative grade, NOT a paper P&L -- see sim net below).
        rows = cur.execute(
            f"""SELECT outcome, realized_adjusted_return
                FROM predictions WHERE status='graded' AND config_version IN {_in(real)}""",
            list(real),
        ).fetchall() if real else []
        resolved = [r["outcome"] for r in rows if r["outcome"] in ("correct", "incorrect")]
        rets = [r["realized_adjusted_return"] for r in rows if r["realized_adjusted_return"] is not None]
        if resolved:
            hit = 100 * sum(1 for o in resolved if o == "correct") / len(resolved)
            print(f"  sentiment-direction (real): resolved n={len(resolved):,}  hit rate {hit:.1f}%  "
                  f"mean adj return {statistics.fmean(rets) * 100:+.2f}%/pred (n_ret={len(rets):,})")
        else:
            print("  sentiment-direction: n/a (no resolved real predictions)")
        # Per real config_version breakdown (so the deck's per-gate n's are derivable).
        print("  by config_version (graded):")
        for row in cur.execute(
            "SELECT config_version, COUNT(*) c FROM predictions WHERE status='graded' GROUP BY config_version ORDER BY c DESC"
        ):
            cv = row["config_version"]
            tag = "baseline" if cv in base else "real"
            g = cur.execute(
                "SELECT SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) k, "
                "SUM(CASE WHEN outcome IN ('correct','incorrect') THEN 1 ELSE 0 END) r "
                "FROM predictions WHERE status='graded' AND config_version=?",
                (cv,),
            ).fetchone()
            hr = f"{100 * g['k'] / g['r']:.1f}%" if g["r"] else "--"
            print(f"     {cv[:12]:12} [{tag:8}] graded {row['c']:>5}  resolved {g['r'] or 0:>5}  hit {hr}")

    # Sim paper-trade net per-trade return (the "~ -1%/trade" figure -- net of cost).
    if has_table(cur, "sim_trades"):
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(sim_trades)")}
        if "net_return" in cols:
            vs = [
                r["net_return"]
                for r in cur.execute("SELECT net_return FROM sim_trades WHERE status='closed' AND net_return IS NOT NULL")
            ]
            if vs:
                print(f"  sim paper trades (closed):  n={len(vs):,}  mean NET return "
                      f"{statistics.fmean(vs) * 100:+.2f}%/trade  (gross vs net split in docs/results_methods.md)")
    else:
        print("  sim net/trade: n/a (sim_trades absent)")

    # High-alert vs control next-day |move| expansion, from matured observations.
    if has_table(cur, "signal_observations"):
        hi: list[float] = []
        lo: list[float] = []
        for r in cur.execute(
            "SELECT features_json, marks_json FROM signal_observations WHERE status='matured'"
        ):
            try:
                f = json.loads(r["features_json"]) if isinstance(r["features_json"], str) else (r["features_json"] or {})
                m = json.loads(r["marks_json"]) if isinstance(r["marks_json"], str) else (r["marks_json"] or {})
            except (ValueError, TypeError):
                continue
            car = m.get("car_1d")
            if car is None:
                continue
            (hi if f.get("high_alert") else lo).append(abs(car))
        if hi and lo:
            exp = 100 * (statistics.fmean(hi) / statistics.fmean(lo) - 1)
            print(f"  high-alert |1d move|:  high n={len(hi):,} mean {statistics.fmean(hi) * 100:.2f}%  "
                  f"vs control n={len(lo):,} mean {statistics.fmean(lo) * 100:.2f}%  -> expansion {exp:+.1f}%")
        else:
            print("  high-alert expansion: n/a (no matured observations with car_1d)")
    else:
        print("  high-alert expansion: n/a (signal_observations absent -- use --source for the full DB)")


def _count_lines(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += sum(1 for _ in p.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    return total


def section_repo() -> None:
    print("\nSTATIC REPO STATS (recomputed from the tree)")
    # tests
    tests = 0
    for p in (REPO / "tests").rglob("*.py"):
        tests += len(re.findall(r"^\s*(?:async\s+)?def test_", p.read_text(encoding="utf-8", errors="ignore"), re.M))
    # endpoints (app.py route decorators)
    app = (REPO / "src" / "pipeline" / "api" / "app.py").read_text(encoding="utf-8", errors="ignore")
    endpoints = len(re.findall(r"@app\.(?:get|post|put|delete|patch)\(", app))
    # frontend routes (page.tsx)
    routes = len(list((REPO / "frontend" / "src" / "app").rglob("page.tsx")))
    # DB tables (models.py)
    models = (REPO / "src" / "pipeline" / "common" / "models.py").read_text(encoding="utf-8", errors="ignore")
    tables = len(re.findall(r"__tablename__\s*=", models))
    # LOC
    py = list((REPO / "src").rglob("*.py")) + list((REPO / "backend").rglob("*.py")) + list((REPO / "scripts").rglob("*.py"))
    py = [p for p in py if "__pycache__" not in p.parts]
    ts = list((REPO / "frontend" / "src").rglob("*.ts")) + list((REPO / "frontend" / "src").rglob("*.tsx"))
    py_loc, ts_loc = _count_lines(py), _count_lines(ts)
    print(f"  test functions         {tests}   (deck ~{DECK['tests']})")
    print(f"  API endpoints          {endpoints}   (deck {DECK['endpoints']})   [app.py route decorators]")
    print(f"  frontend routes        {routes}")
    print(f"  DB tables              {tables}   (deck {DECK['tables']})   [models.py __tablename__]")
    print(f"  Python LOC             {py_loc:,}  ({len(py)} files)")
    print(f"  TypeScript LOC         {ts_loc:,}  ({len(ts)} files)")
    print(f"  total LOC              {py_loc + ts_loc:,}   (deck ~{DECK['loc_k']}k)")
    print("  a11y (WCAG 2.1 AA)     462 -> 0 critical+serious   [docs/ada_compliance.md; re-run: cd frontend && npm run a11y]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="path to a pipeline.db (default: DATABASE_URL / data/pipeline.db)")
    ap.add_argument("--latency-window", type=int, default=8000, help="most-recent N scored clusters for the latency sample")
    args = ap.parse_args()

    db = resolve_db_path(args.source)
    print("=" * 68)
    print("DECK STATS RECEIPTS  (read-only)")
    print(f"DB: {db}")
    print("=" * 68)
    try:
        con = connect_ro(db)
    except sqlite3.OperationalError as e:
        print(f"cannot open DB read-only: {e}")
        # repo stats still work without a DB
        section_repo()
        return
    cur = con.cursor()
    section_ledger(cur)
    section_throughput(cur)
    section_latency(cur, args.latency_window)
    section_cohorts(cur)
    con.close()
    section_repo()
    print("\nDefinitions + sources for every figure: docs/results_methods.md")


if __name__ == "__main__":
    main()
