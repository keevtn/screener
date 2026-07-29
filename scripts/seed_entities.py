"""Seed the entities table from SEC company_tickers.json (docs/ROADMAP.md task 0.3).

Fetches the authoritative CIK<->ticker file, folds dual-class listings, attaches
mechanical + manual aliases, and upserts one row per company into ``entities``.
Watchlist members (configs/watchlist.txt) are marked active; universe-criteria
refinement of the active flag is task 0.6.

Usage:
    python scripts/seed_entities.py [--url DATABASE_URL] [--from-file company_tickers.json]

ROADMAP-NOTE: new code uses httpx (roadmap 0/1). The legacy backend/edgar_tickers.py
(aiohttp, in-memory only) is left untouched until its Phase 1 refit.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.config_files import load_aliases, load_watchlist
from pipeline.common.db import make_engine
from pipeline.common.entities import build_entities
from pipeline.common.models import Entity

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC fair-access wants a declared contact UA (roadmap I9 / task 1.3).
_UA = os.environ.get("EDGAR_USER_AGENT") or os.environ.get(
    "SEC_CONTACT_EMAIL", "Market-News-Pipeline set-EDGAR_USER_AGENT@example.com"
)


def fetch_company_tickers(*, timeout: float = 20.0) -> Any:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    resp = httpx.get(SEC_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _rows(raw: Any) -> list[dict[str, Any]]:
    return list(raw.values()) if isinstance(raw, dict) else list(raw or [])


def upsert_entities(session: Session, records: list[dict[str, Any]], watchlist: set[str]) -> int:
    """Insert-or-replace entities. Idempotent on ticker PK (safe to re-seed)."""
    for rec in records:
        rec = dict(rec)
        if rec["ticker"] in watchlist:
            rec["active"] = True
        stmt = sqlite_insert(Entity).values(**rec)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Entity.ticker],
            set_={
                "cik": stmt.excluded.cik,
                "canonical_name": stmt.excluded.canonical_name,
                "aliases_json": stmt.excluded.aliases_json,
                "cashtag": stmt.excluded.cashtag,
                "exchange": stmt.excluded.exchange,
                "active": stmt.excluded.active,
            },
        )
        session.execute(stmt)
    session.commit()
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    parser.add_argument(
        "--from-file", default=None, help="local company_tickers.json (skips network)"
    )
    args = parser.parse_args()

    if args.from_file:
        raw = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        raw = fetch_company_tickers()
    records = build_entities(_rows(raw), alias_overrides=load_aliases())
    watchlist = set(load_watchlist())

    engine = make_engine(args.url)
    with Session(engine) as session:
        n = upsert_entities(session, records, watchlist)
    print(
        f"seeded {n} entities into {engine.url} ({len(watchlist)} watchlist members marked active)"
    )


if __name__ == "__main__":
    main()
