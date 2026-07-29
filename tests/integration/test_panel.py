"""Gate 5b: scheduled lifecycle, lockup, cold-start, presets, and the panel API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Entity,
    RawItem,
    ScheduledEvent,
)
from pipeline.panel import (
    compile_preset,
    compute_lockup_expiry,
    fired_panel,
    load_presets,
    onboard_listing,
    roll_event_status,
    upsert_scheduled_event,
)

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)


def _seed_cluster(
    session, cid, ticker, *, catalyst, stage, materiality, role="subject", published=None,
    scored_at=None,
):
    published = published or (datetime.now(UTC) - timedelta(hours=1))  # past-relative for recency
    scored_at = scored_at or published  # default: call made at publish time
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=published,
            ingested_at=published,
            payload_json={"title": f"{ticker} {catalyst}", "guid": cid},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=1,
            member_count=1,
            created_at=published,
        )
    )
    session.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=0.3,
            lm_score=0.1,
            text_kind="article",
            catalyst_type=catalyst,
            event_stage=stage,
            materiality=materiality,
            high_alert=materiality >= 0.7,
            predictive=True,
            reaction_dependent=False,
            created_at=scored_at,
        )
    )
    session.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role=role,
            match_method="name",
            created_at=published,
        )
    )


# --- 5b.3 presets ------------------------------------------------------------


def test_preset_compiles_to_filter():
    compiled = compile_preset(
        {"catalyst_types": ["ma"], "stages": ["announced"], "min_materiality": 0.6}
    )
    hand_built = {
        "catalyst_types": ["ma"],
        "stages": ["announced"],
        "min_materiality": 0.6,
        "high_alert_only": False,
        "min_abs_sentiment": 0.0,  # shared field for the Phase 7 extreme_sentiment term
    }
    assert compiled == hand_built


def test_preset_zero_code_new_type():
    # A brand-new preset (never seen in code) compiles the same way (I11).
    compiled = compile_preset({"catalyst_types": ["spinoff", "buyback"], "min_materiality": 0.3})
    assert compiled["catalyst_types"] == ["buyback", "spinoff"]  # normalized/sorted
    assert compiled["stages"] is None and compiled["min_materiality"] == 0.3


def test_shipped_presets_load():
    presets = load_presets()
    assert {
        "earnings_drift",
        "ma_watch",
        "dilution_radar",
        "insider_conviction",
        "squeeze_watch",
        "ipo_watch",
    } <= set(presets)


def test_ipo_watch_preset_compiles_over_ipo_family():
    # IPO-family bundle: existing catalyst types only, no new filter term (I11).
    # The 0.40 gate must admit lockup_expiry (materiality 0.40) as well as ipo.
    presets = load_presets()
    compiled = compile_preset(presets["ipo_watch"])
    assert compiled["catalyst_types"] == ["ipo", "lockup_expiry"]  # sorted
    assert compiled["stages"] is None  # open across scheduled/confirmed
    assert compiled["min_materiality"] == 0.40


# --- 5b.1 / 5b.2 scheduled + lockup + cold start -----------------------------


def test_lockup_computed():
    assert compute_lockup_expiry(date(2026, 1, 1)) == date(2026, 6, 30)  # +180 days


def test_cold_start_and_lockup_on_onboard(engine):
    with Session(engine) as s:
        s.add(Entity(ticker="NEW", canonical_name="NewCo Inc.", aliases_json=[], active=True))
        s.commit()
        expiry = onboard_listing(s, "NEW", date(2026, 1, 1), now=NOW)
        assert expiry == date(2026, 6, 30)
        assert s.get(Entity, "NEW").cold_start_until == date(2026, 1, 31)  # +30 days
        ev = s.execute(select(ScheduledEvent).where(ScheduledEvent.ticker == "NEW")).scalar_one()
        assert ev.catalyst_type == "lockup_expiry" and ev.event_date == date(2026, 6, 30)


def test_scheduled_event_lifecycle(engine):
    with Session(engine) as s:
        upsert_scheduled_event(s, "AAPL", "earnings_results", date(2026, 7, 1), now=NOW)  # past
        upsert_scheduled_event(s, "MSFT", "earnings_results", date(2026, 8, 1), now=NOW)  # future
        cancelled = ScheduledEvent(
            ticker="TSLA",
            catalyst_type="ma",
            event_date=date(2026, 6, 1),
            source="x",
            status="cancelled",
            meta_json={},
            created_at=NOW,
        )
        s.add(cancelled)
        s.commit()

        rolled = roll_event_status(s, now=NOW)
        assert rolled == 1  # only the past AAPL event rolls
        from sqlalchemy import select

        by_ticker = {e.ticker: e.status for e in s.execute(select(ScheduledEvent)).scalars()}
        assert by_ticker == {"AAPL": "passed", "MSFT": "upcoming", "TSLA": "cancelled"}


# --- 5b.4 panel API ----------------------------------------------------------


def test_catalysts_fired_ranked_with_roles(engine):
    with Session(engine) as s:
        _seed_cluster(
            s, "ma1", "MSFT", catalyst="ma", stage="announced", materiality=0.9, role="acquirer"
        )
        _seed_cluster(
            s, "ma2", "ATVI", catalyst="ma", stage="announced", materiality=0.9, role="target"
        )
        _seed_cluster(s, "low", "XYZ", catalyst="product_pricing", stage=None, materiality=0.2)
        s.commit()
    body = TestClient(create_app(engine)).get("/catalysts/fired").json()
    assert body["count"] == 3
    # Highest materiality first; M&A carries role badges.
    top = body["items"][0]
    assert top["catalyst_type"] == "ma"
    # Fired items carry the origin item's source URL so the tape can link through.
    urls = {it["cluster_id"]: it["url"] for it in body["items"]}
    assert urls["ma1"] == "https://x/ma1"
    roles = {t["ticker"]: t["role"] for it in body["items"] for t in it["tickers"]}
    assert roles["MSFT"] == "acquirer" and roles["ATVI"] == "target"
    # Two-axis sentiment rides along so the fired rows can show a tape-style tone
    # badge (finbert primary, lm secondary; kept separate per I7).
    assert top["finbert_score"] == 0.3 and top["lm_score"] == 0.1
    assert "finbert_label" in top  # present even when the scorer left it null


def test_fired_and_ticker_clusters_expose_call_time(engine):
    """called_at = when the system scored the cluster (the call), distinct from
    the article's publish time, and stable across idempotent re-scores."""
    pub = datetime.now(UTC) - timedelta(hours=3)
    called = pub + timedelta(minutes=47)  # sweep interval + ingest lag
    with Session(engine) as s:
        _seed_cluster(
            s, "c1", "NVDA", catalyst="ma", stage="announced", materiality=0.9,
            published=pub, scored_at=called,
        )
        s.commit()
    client = TestClient(create_app(engine))

    fired = client.get("/catalysts/fired").json()["items"][0]
    assert fired["published_at"] == pub.isoformat()
    assert fired["called_at"] == called.isoformat()

    tick = client.get("/tickers/NVDA/clusters").json()["items"][0]
    assert tick["called_at"] == called.isoformat()
    assert tick["published_at"] == pub.isoformat()

    # Re-scoring upserts must NOT reset the original call time.
    from pipeline.score.score import ClusterScoreValues, persist_cluster_score

    with Session(engine) as s:
        persist_cluster_score(
            s,
            ClusterScoreValues(
                cluster_id="c1", finbert_score=0.5, lm_score=0.2, finbert_label="bullish",
                text_kind="article", catalyst_type="ma", event_stage="announced",
                materiality=0.9, direction_hint=None, high_alert=True, predictive=True,
                reaction_dependent=False,
            ),
        )
        s.commit()
    refired = client.get("/catalysts/fired").json()["items"][0]
    assert refired["called_at"] == called.isoformat()  # preserved, not utcnow()


def test_scheduled_endpoint_countdowns(engine):
    with Session(engine) as s:
        upsert_scheduled_event(s, "NVDA", "earnings_results", date(2026, 7, 20), now=NOW)
        upsert_scheduled_event(s, "AAPL", "earnings_results", date(2026, 6, 1), now=NOW)  # past
        roll_event_status(s, now=NOW)
    # scheduled_panel uses utcnow() for "today"; the past event is already rolled to passed.
    body = TestClient(create_app(engine)).get("/catalysts/scheduled").json()
    tickers = {i["ticker"] for i in body["items"]}
    assert "AAPL" not in tickers  # passed, excluded
    if "NVDA" in tickers:  # future relative to real now
        nvda = next(i for i in body["items"] if i["ticker"] == "NVDA")
        assert nvda["days_until"] >= 0


def test_presets_and_screener_endpoints(engine):
    with Session(engine) as s:
        _seed_cluster(s, "ma1", "MSFT", catalyst="ma", stage="announced", materiality=0.9)
        _seed_cluster(
            s, "off", "DILU", catalyst="secondary_offering", stage="announced", materiality=0.7
        )
        s.commit()
    client = TestClient(create_app(engine))

    presets = client.get("/presets").json()
    assert "ma_watch" in presets["presets"]

    ma = client.get("/screener", params={"preset": "ma_watch"}).json()
    assert [i["cluster_id"] for i in ma["items"]] == ["ma1"]  # only the ma cluster
    dil = client.get("/screener", params={"preset": "dilution_radar"}).json()
    assert [i["cluster_id"] for i in dil["items"]] == ["off"]
    assert client.get("/screener", params={"preset": "nope"}).status_code == 404


def test_fired_window_days_and_ordering(engine):
    """window_days bounds the feed; order=recent is newest-first (surfaces the
    whole week), order=rank is materiality×recency (curated)."""
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        # A: newest but minor; B: a day old but major; C: 20 days old (out of a 1w window)
        _seed_cluster(s, "A", "AAA", catalyst="ma", stage="announced", materiality=0.20,
                      published=now - timedelta(hours=1))
        _seed_cluster(s, "B", "BBB", catalyst="ma", stage="announced", materiality=0.95,
                      published=now - timedelta(hours=24))
        _seed_cluster(s, "C", "CCC", catalyst="ma", stage="announced", materiality=0.90,
                      published=now - timedelta(days=20))
        s.commit()

        recent = fired_panel(s, now=now, window_days=7, order="recent")
        ids = [r["cluster_id"] for r in recent]
        assert "C" not in ids          # 20d old -> outside the 1-week window
        assert ids == ["A", "B"]       # newest-first

        ranked = [r["cluster_id"] for r in fired_panel(s, now=now, window_days=7, order="rank")]
        assert ranked == ["B", "A"]    # older major out-ranks the fresh minor

        wide = [r["cluster_id"] for r in fired_panel(s, now=now, window_days=30, order="recent")]
        assert "C" in wide             # a wider window admits the old event
