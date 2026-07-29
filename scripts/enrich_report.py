"""Top-N recent clusters with origin, tier, and attributed tickers (Gate 2 eyeball).

python scripts/enrich_report.py [--url DATABASE_URL] [--limit 20]
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import Cluster, ClusterEntity, RawItem, UnmappedMention


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    engine = make_engine(args.url)
    with Session(engine) as session:
        clusters = (
            session.execute(select(Cluster).order_by(Cluster.created_at.desc()).limit(args.limit))
            .scalars()
            .all()
        )
        total_clusters = session.execute(select(func.count()).select_from(Cluster)).scalar_one()
        total_unmapped = session.execute(
            select(func.count()).select_from(UnmappedMention)
        ).scalar_one()
        total_attrib = session.execute(select(func.count()).select_from(ClusterEntity)).scalar_one()

        print(f"clusters={total_clusters} attributions={total_attrib} unmapped={total_unmapped}")
        denom = total_attrib + total_unmapped
        print(f"unmapped_rate={(total_unmapped / denom if denom else 0):.1%}\n")

        for cl in clusters:
            origin = session.get(RawItem, cl.origin_item_id)
            title = (origin.payload_json.get("title", "") if origin else "")[:70]
            ents = (
                session.execute(
                    select(ClusterEntity.ticker, ClusterEntity.ticker_role).where(
                        ClusterEntity.cluster_id == cl.cluster_id
                    )
                )
            ).all()
            tickers = ", ".join(f"{t}[{r}]" for t, r in ents) or "-"
            src = origin.source if origin else "?"
            print(
                f"[tier {cl.origin_tier}] x{cl.member_count:<2} {src[:22]:22} {tickers:20} {title}"
            )


if __name__ == "__main__":
    main()
