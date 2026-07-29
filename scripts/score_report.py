"""Score every cluster (both axes) and print recent clusters with sentiment +
catalyst columns (Gate 3 eyeball).  docs/ROADMAP.md Phase 3.

    python scripts/score_report.py [--url DATABASE_URL] [--limit 20] [--finbert]

By default sentiment uses the zero-dependency L-M lexicon; --finbert also loads
FinBERT (needs torch/transformers, ~440MB).
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import ClusterScore, RawItem
from pipeline.score.score import score_clusters
from pipeline.score.sentiment import default_finbert, default_lm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--finbert", action="store_true", help="also load FinBERT")
    args = parser.parse_args()

    engine = make_engine(args.url)
    lm = default_lm()
    finbert = default_finbert() if args.finbert else None

    with Session(engine) as session:
        n = score_clusters(session, finbert=finbert, lm=lm)
        print(f"scored {n} clusters\n")

        high = session.execute(
            select(func.count()).select_from(ClusterScore).where(ClusterScore.high_alert)
        ).scalar_one()
        with_cat = session.execute(
            select(func.count())
            .select_from(ClusterScore)
            .where(ClusterScore.catalyst_type.is_not(None))
        ).scalar_one()
        print(f"clusters with a catalyst: {with_cat} | high_alert: {high}\n")

        rows = (
            session.execute(
                select(ClusterScore).order_by(ClusterScore.materiality.desc()).limit(args.limit)
            )
            .scalars()
            .all()
        )
        for cs in rows:
            origin = session.get(RawItem, cs.cluster_id)
            title = (origin.payload_json.get("title", "") if origin else "")[:56]
            cat = f"{cs.catalyst_type}/{cs.event_stage}" if cs.catalyst_type else "-"
            fb = f"{cs.finbert_score:+.2f}" if cs.finbert_score is not None else "  -  "
            lm_s = f"{cs.lm_score:+.2f}" if cs.lm_score is not None else "  -  "
            alert = "!" if cs.high_alert else " "
            print(
                f"{alert} m={cs.materiality:.2f} {cat:26} {cs.text_kind:13} "
                f"fb={fb} lm={lm_s} dir={cs.direction_hint or '-':18} {title}"
            )


if __name__ == "__main__":
    main()
