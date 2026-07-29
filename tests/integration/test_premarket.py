"""PMR integration: panel ranking, snapshot freeze, report cards, API route."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    ImmutableRowViolation,
    PremarketPanel,
    RawItem,
    ScheduledEvent,
)
from pipeline.marketdata import TradingCalendar
from pipeline.panel import (
    grade_premarket_panels,
    persist_premarket_snapshot,
    premarket_panel,
)

# Tuesday 2026-07-21 premarket, 08:35 ET (EDT -> 12:35 UTC).
NOW = datetime(2026, 7, 21, 12, 35, tzinfo=UTC)
CAL = TradingCalendar([date(2026, 7, d) for d in (16, 17, 20, 21, 22, 23)])
OVERNIGHT = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)  # Mon 9pm ET — inside the window


def _seed(s, cid, ticker, *, catalyst="earnings_results", materiality=0.5,
          high_alert=False, finbert=0.0, hint=None, role="subject",
          published=OVERNIGHT, source_class="structured"):
    s.add(RawItem(id=cid, source="Reuters", source_class=source_class, url=f"https://x/{cid}",
                  published_at=published, ingested_at=published,
                  payload_json={"title": f"{ticker} {catalyst} headline", "guid": cid}))
    s.flush()
    s.add(Cluster(cluster_id=cid, origin_item_id=cid, member_ids_json=[cid], origin_tier=1,
                  member_count=1, created_at=published))
    s.add(ClusterScore(cluster_id=cid, finbert_score=finbert, lm_score=finbert,
                       text_kind="article", catalyst_type=catalyst, event_stage="confirmed",
                       materiality=materiality, high_alert=high_alert, predictive=True,
                       direction_hint=hint, reaction_dependent=False, created_at=published))
    s.add(ClusterEntity(cluster_id=cid, ticker=ticker, ticker_role=role,
                        match_method="name", created_at=published))
    s.flush()


def test_panel_ranking_boosts_and_flags(engine):
    with Session(engine) as s:
        # FDA + high-alert beats a plain earnings story at equal materiality...
        _seed(s, "c1", "BIOX", catalyst="fda_action", materiality=0.5, high_alert=True,
              finbert=-0.8)
        _seed(s, "c2", "PLAIN", catalyst="earnings_results", materiality=0.5, finbert=0.1)
        # ...an offering leans short regardless of its (positive) sentiment:
        _seed(s, "c3", "DILU", catalyst="secondary_offering", materiality=0.4, finbert=0.6)
        # Outside the window / social — must not appear:
        _seed(s, "c4", "OLD", published=datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        _seed(s, "c5", "SOC", source_class="social")
        # Earnings scheduled today: boosts PLAIN, and creates a scheduled-only row
        # for GHOST (no overnight news at all).
        for tk in ("PLAIN", "GHOST"):
            s.add(ScheduledEvent(ticker=tk, catalyst_type="earnings_results",
                                 event_date=date(2026, 7, 21), stage="scheduled",
                                 source="finviz", status="upcoming", meta_json={},
                                 created_at=NOW - timedelta(days=1)))
        s.commit()

        rows = premarket_panel(s, CAL, NOW)
        by = {r.ticker: r for r in rows}
        assert set(by) == {"BIOX", "PLAIN", "DILU", "GHOST"}
        assert by["BIOX"].score > by["DILU"].score  # high_alert + fda outrank offering
        assert by["PLAIN"].earnings_today and not by["PLAIN"].scheduled_only
        assert by["GHOST"].scheduled_only and by["GHOST"].n_clusters == 0
        assert by["DILU"].lean == "short" and by["BIOX"].lean == "short"
        assert by["PLAIN"].lean == "none"  # |0.1| below the conviction bar
        # Deterministic: same inputs, same order.
        assert [r.ticker for r in premarket_panel(s, CAL, NOW)] == [r.ticker for r in rows]


def test_snapshot_freezes_once_and_only_in_window(engine):
    with Session(engine) as s:
        _seed(s, "c1", "AAA", high_alert=True)
        s.commit()
        assert persist_premarket_snapshot(s, CAL, NOW).startswith("frozen")
        assert persist_premarket_snapshot(s, CAL, NOW) == "snapshot exists"
        # Outside 08:30-09:30 ET and weekends: no-ops.
        late = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)  # 10:00 ET
        assert "skipped" in persist_premarket_snapshot(s, CAL, late)
        sat = datetime(2026, 7, 18, 12, 35, tzinfo=UTC)
        assert "skipped" in persist_premarket_snapshot(s, CAL, sat)


def test_frozen_panel_rejects_rank_edits_allows_grading(engine):
    with Session(engine) as s:
        _seed(s, "c1", "AAA")
        s.commit()
        persist_premarket_snapshot(s, CAL, NOW)
        panel = s.get(PremarketPanel, date(2026, 7, 21))
        panel.rows_json = []  # tampering with the frozen ranking
        with pytest.raises(ImmutableRowViolation):
            s.commit()
        s.rollback()
        panel = s.get(PremarketPanel, date(2026, 7, 21))
        panel.graded_at = NOW  # report-card fields are the sanctioned mutation
        s.commit()


def _frames(prices: dict[str, list[tuple[date, float, float]]]) -> dict[str, pd.DataFrame]:
    """{ticker: [(date, open, adj_close)]} -> provider frames."""
    return {
        t: pd.DataFrame(
            {"date": [pd.Timestamp(d) for d, _, _ in rows],
             "open": [o for _, o, _ in rows],
             "adj_close": [c for _, _, c in rows]}
        )
        for t, rows in prices.items()
    }


def test_report_card_grades_leans_and_summary(engine, make_provider):
    sd = date(2026, 7, 21)
    with Session(engine) as s:
        # UPP leans long and rises (hit); DWN leans short and falls (hit);
        # NOBAR never gets bars (delisted) — graded around once the panel ages.
        _seed(s, "c1", "UPP", materiality=0.9, high_alert=True, finbert=0.8)
        _seed(s, "c2", "DWN", catalyst="secondary_offering", materiality=0.6)
        _seed(s, "c3", "NOBAR", materiality=0.3, finbert=0.5)
        s.commit()
        persist_premarket_snapshot(s, CAL, NOW)

        provider = make_provider(_frames({
            "UPP": [(date(2026, 7, 20), 0.0, 100.0), (sd, 100.0, 110.0)],  # +10% oc
            "DWN": [(date(2026, 7, 20), 0.0, 50.0), (sd, 51.0, 45.9)],  # -10% oc, gap up
        }))

        # Same evening, partial coverage (NOBAR missing), age 0 -> held for retry.
        eve = datetime(2026, 7, 21, 21, 0, tzinfo=UTC)  # 17:00 ET
        assert "graded 0" in grade_premarket_panels(s, provider, CAL, eve)
        # Two trading days later: grade with what exists.
        later = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
        assert "graded 1" in grade_premarket_panels(s, provider, CAL, later)

        panel = s.get(PremarketPanel, sd)
        assert panel.graded_at is not None
        oc = panel.outcomes_json
        assert oc["UPP"]["lean_hit"] is True and oc["UPP"]["oc_return"] == pytest.approx(0.10)
        assert oc["DWN"]["lean_hit"] is True and oc["DWN"]["oc_return"] == pytest.approx(-0.10)
        assert oc["DWN"]["gap_return"] == pytest.approx(0.02)
        assert "NOBAR" not in oc
        assert panel.summary_json["graded_n"] == 2
        assert panel.summary_json["lean_hit_rate"] == 1.0
        # Idempotent: nothing left to grade.
        assert "graded 0" in grade_premarket_panels(s, provider, CAL, later)


def test_report_card_closes_out_holiday_panel(engine, make_provider):
    with Session(engine) as s:
        _seed(s, "c1", "AAA")
        s.commit()
        persist_premarket_snapshot(s, CAL, NOW)
        provider = make_provider({})  # no bars anywhere: a holiday-shaped session
        later = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
        assert "graded 1" in grade_premarket_panels(s, provider, CAL, later)
        panel = s.get(PremarketPanel, date(2026, 7, 21))
        assert panel.summary_json["graded_n"] == 0 and panel.outcomes_json == {}


def test_api_route_serves_stored_and_flags_stale(engine):
    with Session(engine) as s:
        _seed(s, "c1", "AAA", high_alert=True)
        s.commit()
        persist_premarket_snapshot(s, CAL, NOW)
    client = TestClient(create_app(engine))
    body = client.get("/catalysts/premarket").json()
    assert body["available"] is True and body["live"] is False
    assert body["session_date"] == "2026-07-21"
    assert body["count"] == 1 and body["rows"][0]["ticker"] == "AAA"
    assert body["graded"] is False
    # stale is relative to the real today — a 2026 fixture panel is stale by now.
    assert body["stale"] is True
    # live=1: inside the premarket window it recomputes for TODAY (live, not
    # stale); outside it (or on any failure) it falls back to the stored panel.
    # Assert the contract, not the wall clock — this test runs at any hour.
    r2 = client.get("/catalysts/premarket?live=1").json()
    assert r2["available"] is True
    if r2["live"]:
        assert r2["stale"] is False and r2["graded"] is False
    else:
        assert r2["session_date"] == "2026-07-21"


def test_api_route_empty_state(engine):
    client = TestClient(create_app(engine))
    body = client.get("/catalysts/premarket").json()
    assert body == {"available": False, "live": False, "rows": [], "count": 0}
