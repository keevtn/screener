"""Task 0.3 gate test: entity backbone — mapping, aliases, dual-class collapse."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.entities import build_entities, mechanical_alias, resolve_ticker
from pipeline.common.models import Entity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "company_tickers_sample.json"
ALIAS_OVERRIDES = {"Alphabet": "GOOGL", "Meta": "META", "Facebook": "META"}


def _rows():
    return list(json.loads(FIXTURE.read_text(encoding="utf-8")).values())


def _entities():
    return build_entities(_rows(), alias_overrides=ALIAS_OVERRIDES)


def test_mechanical_alias_strips_designators():
    assert mechanical_alias("Apple Inc.") == "Apple"
    assert mechanical_alias("Meta Platforms, Inc.") == "Meta Platforms"
    assert mechanical_alias("BERKSHIRE HATHAWAY INC") == "BERKSHIRE HATHAWAY"
    assert mechanical_alias("The Coca-Cola Company") == "Coca-Cola"


def test_aapl_maps_with_padded_cik():
    ents = {e["ticker"]: e for e in _entities()}
    aapl = ents["AAPL"]
    assert aapl["cik"] == "0000320193"
    assert aapl["canonical_name"] == "Apple Inc."
    assert aapl["cashtag"] == "$AAPL"
    assert aapl["active"] is True
    assert "Apple" in aapl["aliases_json"]


def test_dual_class_collapses_to_one_canonical():
    ents = {e["ticker"]: e for e in _entities()}
    # First-row-wins: GOOGL/BRK-B canonical; GOOG/BRK-A fold in as aliases.
    assert "GOOGL" in ents and "GOOG" not in ents
    assert "BRK-B" in ents and "BRK-A" not in ents
    assert "GOOG" in ents["GOOGL"]["aliases_json"]
    assert "BRK-A" in ents["BRK-B"]["aliases_json"]
    # Exactly one record per CIK.
    ciks = [e["cik"] for e in _entities()]
    assert len(ciks) == len(set(ciks))


def test_resolve_alphabet_via_mechanical_and_meta_via_override():
    ents = _entities()
    assert resolve_ticker("Alphabet", ents) == "GOOGL"  # mechanical alias
    assert resolve_ticker("Meta", ents) == "META"  # manual override only
    assert resolve_ticker("Facebook", ents) == "META"  # manual override
    assert resolve_ticker("AAPL", ents) == "AAPL"  # exact ticker
    assert resolve_ticker("Nonexistent Co", ents) is None


def test_seed_entities_upsert_idempotent(session: Session):
    from scripts.seed_entities import upsert_entities

    records = _entities()
    watchlist = {"AAPL", "GOOGL"}
    upsert_entities(session, records, watchlist)
    first = session.execute(select(Entity)).scalars().all()
    assert len(first) == len(records)

    # Re-seed: no duplicate rows (upsert on ticker PK).
    upsert_entities(session, records, watchlist)
    second = session.execute(select(Entity)).scalars().all()
    assert len(second) == len(records)

    googl = session.get(Entity, "GOOGL")
    assert googl.cik == "0001652044"
    assert "GOOG" in googl.aliases_json
