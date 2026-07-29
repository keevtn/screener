"""Run enrichment (cluster + resolve) over the archive (docs/ROADMAP.md task 2.5).

Idempotent: safe to re-run. Loads entities from the DB to build the resolver and
configs/source_tiers.yaml for origin tiering.

    python scripts/backfill.py [--url DATABASE_URL]
"""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import Entity
from pipeline.enrich.backfill import backfill_enrichment
from pipeline.enrich.resolve import EntityResolver
from pipeline.enrich.tiers import load_source_tiers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    args = parser.parse_args()

    engine = make_engine(args.url)
    tiers = load_source_tiers()
    with Session(engine) as session:
        entities = session.execute(select(Entity)).scalars().all()
        resolver = EntityResolver(entities)
        stats = backfill_enrichment(session, resolver=resolver, tier_of=tiers.tier_of)

    print(
        f"items={stats.items} clusters={stats.clusters} "
        f"attributions={stats.attributions} unmapped={stats.unmapped} "
        f"suppressed={stats.suppressed} unmapped_rate={stats.unmapped_rate:.1%}"
    )
    if not entities:
        print("  (no entities seeded — run scripts/seed_entities.py first for attributions)")


if __name__ == "__main__":
    main()
