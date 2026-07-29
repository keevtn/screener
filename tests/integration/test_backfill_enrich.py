"""Gate 2 task 2.5: idempotent enrichment backfill over a persisted archive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterEntity, RawItem, UnmappedMention
from pipeline.enrich.backfill import backfill_enrichment
from pipeline.enrich.resolve import EntityResolver
from pipeline.enrich.tiers import load_source_tiers

BASE = datetime(2025, 3, 12, 10, 0, tzinfo=UTC)
ENTITIES = [
    {"ticker": "MSFT", "canonical_name": "Microsoft Corp", "aliases_json": ["Microsoft"]},
    {
        "ticker": "ATVI",
        "canonical_name": "Activision Blizzard, Inc.",
        "aliases_json": ["Activision", "Activision Blizzard"],
    },
    {"ticker": "AAPL", "canonical_name": "Apple Inc.", "aliases_json": ["Apple"]},
]


def _raw(id_, source, title, minutes, desc=""):
    return RawItem(
        id=id_,
        source=source,
        source_class="structured",
        url=f"https://x/{id_}",
        published_at=BASE + timedelta(minutes=minutes),
        ingested_at=BASE + timedelta(minutes=minutes),
        payload_json={"title": title, "description": desc, "guid": id_},
    )


def _seed(session):
    ma = "Microsoft to Acquire Activision Blizzard"
    session.add_all(
        [
            # A syndicated M&A story across three outlets (one cluster).
            _raw("bw", "Business Wire", ma, 0),
            _raw("rt", "Reuters", ma, 5),
            _raw("yh", "Yahoo Finance", ma, 10),
            # A separate single-company story.
            _raw("ap", "Reuters", "Apple reports record Q3 earnings", 30),
            # A garbage cashtag -> unmapped.
            _raw("zz", "Reddit - wsb", "$ZZZZ to the moon", 40, desc="buy $ZZZZ now"),
        ]
    )
    session.commit()


def _run(session):
    return backfill_enrichment(
        session, resolver=EntityResolver(ENTITIES), tier_of=load_source_tiers().tier_of
    )


def test_backfill_clusters_and_roles(engine):
    with Session(engine) as s:
        _seed(s)
        stats = _run(s)

        # 3 clusters: the syndicated M&A trio, the Apple story, the $ZZZZ post.
        assert stats.clusters == 3
        assert stats.items == 5

        ma_cluster = s.get(Cluster, "bw")  # origin = earliest (Business Wire, tier 1)
        assert ma_cluster is not None
        assert ma_cluster.member_count == 3
        assert ma_cluster.origin_tier == 1

        roles = dict(
            s.execute(
                select(ClusterEntity.ticker, ClusterEntity.ticker_role).where(
                    ClusterEntity.cluster_id == "bw"
                )
            ).all()
        )
        assert roles == {"MSFT": "acquirer", "ATVI": "target"}

        # Every raw_item lands in exactly one cluster (I5 precondition).
        member_ids = [
            mid for c in s.execute(select(Cluster)).scalars() for mid in c.member_ids_json
        ]
        assert sorted(member_ids) == ["ap", "bw", "rt", "yh", "zz"]
        assert len(member_ids) == len(set(member_ids))

        # The $ZZZZ cashtag is logged unmapped.
        unmapped = s.execute(select(UnmappedMention.mention, UnmappedMention.reason)).all()
        assert ("$ZZZZ", "no_match") in unmapped


def test_backfill_idempotent(engine):
    with Session(engine) as s:
        _seed(s)
        first = _run(s)
        assert first.clusters == 3
        counts_1 = (
            s.execute(select(func.count()).select_from(Cluster)).scalar_one(),
            s.execute(select(func.count()).select_from(ClusterEntity)).scalar_one(),
            s.execute(select(func.count()).select_from(UnmappedMention)).scalar_one(),
        )
        second = _run(s)
        counts_2 = (
            s.execute(select(func.count()).select_from(Cluster)).scalar_one(),
            s.execute(select(func.count()).select_from(ClusterEntity)).scalar_one(),
            s.execute(select(func.count()).select_from(UnmappedMention)).scalar_one(),
        )
        assert counts_1 == counts_2  # re-run creates no duplicate rows
        # Incremental re-run honestly reports zero NEW work (every item is
        # already a cluster member — membership is the durable watermark).
        assert (second.items, second.clusters) == (0, 0)


def test_backfill_delete_chunks_over_sqlite_variable_limit(engine):
    # A whole-archive backfill has tens of thousands of cluster_ids; the idempotency
    # delete must chunk its IN() below SQLite's ~999 bound-variable limit.
    with Session(engine) as s:
        base = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(1050):
            rid = f"r{i}"
            # Spaced >72h apart so each item is its own cluster (no merges).
            ts = base + timedelta(hours=100 * i)
            s.add(
                RawItem(
                    id=rid,
                    source="Reuters",
                    source_class="structured",
                    url=f"https://x/{rid}",
                    published_at=ts,
                    ingested_at=ts,
                    payload_json={"title": f"h{i}", "guid": rid},
                )
            )
        s.commit()
        assert backfill_enrichment(s).clusters == 1050  # builds >999 clusters
        # Wipe clusters (attributions/unmapped remain) and force the full-rebuild
        # path again: the idempotency delete must chunk its IN() over the >999
        # pre-existing rows (was crashing on SQLite's bound-variable limit).
        from sqlalchemy import delete as sa_delete

        s.execute(sa_delete(Cluster))
        s.commit()
        assert backfill_enrichment(s).clusters == 1050


def test_backfill_incremental_merges_and_creates(engine):
    """Steady-state sweeps: only new items cluster; syndication folds into the
    existing cluster (stable id) and genuinely new stories form new clusters."""
    from datetime import datetime as _dt

    with Session(engine) as s:
        _seed(s)
        _run(s)  # initial full build: 3 clusters, "bw" = the M&A trio

        now = _dt.now(UTC)
        # Late syndication of the M&A story (ingested now, published near BASE).
        s.add(
            RawItem(
                id="late",
                source="MarketWatch",
                source_class="structured",
                url="https://x/late",
                published_at=BASE + timedelta(minutes=20),
                ingested_at=now,
                payload_json={"title": "Microsoft to Acquire Activision Blizzard", "guid": "late"},
            )
        )
        # A genuinely new story.
        s.add(
            RawItem(
                id="fresh",
                source="Reuters",
                source_class="structured",
                url="https://x/fresh",
                published_at=now,
                ingested_at=now,
                payload_json={"title": "Nvidia unveils next-gen data center GPU", "guid": "fresh"},
            )
        )
        s.commit()

        stats = _run(s)
        assert stats.items == 2
        assert stats.clusters == 2  # one merged (changed), one new — untouched seeds omitted

        ma = s.get(Cluster, "bw")
        assert ma.member_count == 4 and "late" in ma.member_ids_json
        assert ma.origin_tier == 1  # origin identity + tier preserved on merge
        assert s.get(Cluster, "late") is None  # no duplicate story cluster (I5)
        assert s.get(Cluster, "fresh") is not None

        # Attributions were rebuilt for the touched cluster, not duplicated.
        roles = dict(
            s.execute(
                select(ClusterEntity.ticker, ClusterEntity.ticker_role).where(
                    ClusterEntity.cluster_id == "bw"
                )
            ).all()
        )
        assert roles == {"MSFT": "acquirer", "ATVI": "target"}
