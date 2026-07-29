"""Gate 2 task 2.2: dedup + near-duplicate clustering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pipeline.enrich.canonical import from_values as canonical
from pipeline.enrich.cluster import build_clusters

BASE = datetime(2025, 3, 12, 10, 0, tzinfo=UTC)


def _item(id_, source, title, minutes=0):
    ts = BASE + timedelta(minutes=minutes)
    return canonical(id=id_, source=source, source_class="structured", title=title, published_at=ts)


def test_cluster_syndication():
    # One wire story republished verbatim by five outlets, staggered timestamps.
    headline = "Acme Corp Announces Record Quarterly Revenue"
    items = [
        _item("i1", "Business Wire", headline, minutes=0),
        _item("i2", "Reuters", headline, minutes=5),
        _item("i3", "Bloomberg", "Acme Corp announces record quarterly revenue.", minutes=10),
        _item("i4", "Yahoo Finance", headline, minutes=15),
        _item("i5", "PR Newswire", headline, minutes=20),
    ]
    clusters = build_clusters(items)
    assert len(clusters) == 1
    assert clusters[0].origin_item_id == "i1"  # earliest published_at
    assert sorted(clusters[0].member_ids) == ["i1", "i2", "i3", "i4", "i5"]


def test_cluster_negative_control():
    # Similar template, different companies -> token_set_ratio 88.9 < 90 -> separate.
    items = [
        _item("a", "Reuters", "Apple reports Q3 earnings beat", minutes=0),
        _item("m", "Bloomberg", "Microsoft reports Q3 earnings beat", minutes=5),
    ]
    clusters = build_clusters(items)
    assert len(clusters) == 2


def test_outside_window_stays_separate():
    headline = "Acme Corp Announces Record Quarterly Revenue"
    items = [
        _item("i1", "Business Wire", headline, minutes=0),
        _item("i2", "Reuters", headline, minutes=73 * 60),  # 73h later, > 72h window
    ]
    clusters = build_clusters(items)
    assert len(clusters) == 2


def test_every_item_one_cluster():
    items = [
        _item("i1", "Business Wire", "Acme Corp Announces Record Quarterly Revenue", 0),
        _item("i2", "Reuters", "Acme Corp Announces Record Quarterly Revenue", 5),
        _item("a", "Reuters", "Apple reports Q3 earnings beat", 10),
        _item("m", "Bloomberg", "Microsoft reports Q3 earnings beat", 15),
        _item("e", "Nasdaq", "", 20),  # empty headline -> its own cluster
    ]
    clusters = build_clusters(items)
    all_members = [mid for c in clusters for mid in c.member_ids]
    assert sorted(all_members) == ["a", "e", "i1", "i2", "m"]  # every item exactly once
    assert len(all_members) == len(set(all_members))  # disjoint


def test_empty_headlines_do_not_merge():
    items = [_item("e1", "S1", "", 0), _item("e2", "S2", "", 5)]
    assert len(build_clusters(items)) == 2


# --- incremental mode (seeds) -------------------------------------------------


def _seed_bucket(cid, headline, minutes=0, member_ids=None, tier=1):
    from pipeline.enrich.canonical import normalize_headline
    from pipeline.enrich.cluster import SeedBucket

    return SeedBucket(
        cluster_id=cid,
        rep_headline=normalize_headline(headline),
        min_pub=BASE + timedelta(minutes=minutes),
        member_ids=member_ids or [cid],
        origin_tier=tier,
    )


def test_seed_merge_keeps_identity():
    headline = "Acme Corp Announces Record Quarterly Revenue"
    seed = _seed_bucket("orig", headline, minutes=0, member_ids=["orig", "syn1"], tier=1)
    new = [_item("late", "MarketWatch", headline, minutes=30)]
    results = build_clusters(new, seeds=[seed])
    assert len(results) == 1
    r = results[0]
    assert r.cluster_id == "orig"  # stable identity — no duplicate story cluster
    assert r.member_ids == ["orig", "syn1", "late"]  # existing members preserved, new appended
    assert r.origin_tier == 1  # origin tier preserved on merge


def test_untouched_seeds_are_omitted():
    seed = _seed_bucket("quiet", "Some Old Story Nobody Syndicates", minutes=0)
    new = [_item("fresh", "Reuters", "Completely Unrelated Nvidia GPU Launch", minutes=10)]
    results = build_clusters(new, seeds=[seed])
    # Only the new story comes back; the untouched seed persists nothing.
    assert [r.cluster_id for r in results] == ["fresh"]


def test_seed_window_retirement():
    headline = "Acme Corp Announces Record Quarterly Revenue"
    seed = _seed_bucket("orig", headline, minutes=0)
    # Same headline but 73h after the seed's origin -> outside the 72h window.
    new = [_item("late", "Reuters", headline, minutes=73 * 60)]
    results = build_clusters(new, seeds=[seed])
    assert [r.cluster_id for r in results] == ["late"]  # separate cluster, seed untouched


def test_seeded_equals_full_rebuild():
    # Incremental (seeds from a prior pass) must land the same membership a
    # full rebuild over all items would produce.
    headline = "Acme Corp Announces Record Quarterly Revenue"
    first = [_item("i1", "Business Wire", headline, 0), _item("i2", "Reuters", headline, 5)]
    later = [
        _item("i3", "Yahoo Finance", headline, 15),
        _item("n1", "Reuters", "Apple reports Q3 earnings beat", 20),
    ]
    full = build_clusters(first + later)

    prior = build_clusters(first)
    seeds = [
        _seed_bucket(c.cluster_id, headline, minutes=0, member_ids=list(c.member_ids), tier=None)
        for c in prior
    ]
    incr = build_clusters(later, seeds=seeds)
    merged = {c.cluster_id: sorted(c.member_ids) for c in incr}
    # Unchanged clusters carry over from the prior pass.
    for c in prior:
        merged.setdefault(c.cluster_id, sorted(c.member_ids))
    assert merged == {c.cluster_id: sorted(c.member_ids) for c in full}
