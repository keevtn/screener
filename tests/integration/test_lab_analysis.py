"""Gate 5c task 5c.4/5c.5: IC, quintile spread, per-ticker cap, holdout, backfill slice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, RawItem, SignalObservation
from pipeline.lab.analysis import load_lab_rows, quintile_spread, spearman_ic

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _rows(feats, rets):
    return [
        {
            "ticker": f"T{i}",
            "t0": NOW,
            "finbert_score": f,
            "marks": {"car_3d": r},
            "backfill": False,
        }
        for i, (f, r) in enumerate(zip(feats, rets, strict=True))
    ]


def test_ic_known_data():
    feats = [i / 10 for i in range(10)]
    # Perfectly monotonic feature->return -> IC ~ +1; reversed -> ~ -1.
    assert spearman_ic(_rows(feats, feats), 3)["ic"] == 1.0
    assert spearman_ic(_rows(feats, list(reversed(feats))), 3)["ic"] == -1.0


def test_quintile_spread_known():
    feats = [i for i in range(10)]
    rets = [i * 0.01 for i in range(10)]
    out = quintile_spread(_rows(feats, rets), 3)
    # q=2: bottom {0,0.01} mean 0.005; top {0.08,0.09} mean 0.085 -> spread 0.08.
    assert out["bottom_mean"] == 0.005
    assert out["top_mean"] == 0.085
    assert out["spread"] == 0.08


# --- DB-backed defaults ------------------------------------------------------


def _matured(session, oid, ticker, t0, *, finbert, car3, backfill=False, clean=True):
    session.add(
        RawItem(
            id=oid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{oid}",
            published_at=t0,
            ingested_at=t0,
            payload_json={"guid": oid},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=oid,
            origin_item_id=oid,
            member_ids_json=[oid],
            origin_tier=2,
            member_count=1,
            created_at=t0,
        )
    )
    session.add(
        SignalObservation(
            observation_id=oid,
            cluster_id=oid,
            ticker=ticker,
            t0=t0,
            features_json={"finbert_score": finbert},
            marks_json={"car_3d": car3},
            clean_window=clean,
            backfill=backfill,
            status="matured",
            created_at=t0,
        )
    )


OLD = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # > holdout -> in default analysis
RECENT = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)  # within holdout -> excluded default


def test_per_ticker_cap(engine):
    # Distinct DAYS: same-day observations are deduped per event (one per
    # ticker+day) since the XAIR fix, so the cap is exercised across real events.
    with Session(engine) as s:
        for i in range(50):
            _matured(
                s, f"hog{i}", "HOG", OLD - timedelta(days=i), finbert=i / 50, car3=i / 100
            )
        s.commit()
        rows = load_lab_rows(s, now=NOW, per_ticker_cap=10)
        assert sum(1 for r in rows if r["ticker"] == "HOG") == 10  # capped


def test_same_day_observations_deduped(engine):
    # The XAIR lesson: N clusters from one event day collapse to one analysis row.
    with Session(engine) as s:
        for i in range(3):
            _matured(s, f"dup{i}", "XAIR", OLD, finbert=0.5, car3=0.1)
        s.commit()
        rows = load_lab_rows(s, now=NOW)
        assert sum(1 for r in rows if r["ticker"] == "XAIR") == 1


def test_suspect_marks_excluded(engine):
    with Session(engine) as s:
        _matured(s, "glitch", "XAIR", OLD, finbert=0.5, car3=17.8)  # split-break CAR
        _matured(s, "sane", "MSFT", OLD, finbert=0.5, car3=0.05)
        s.commit()
        rows = load_lab_rows(s, now=NOW)
        assert {r["ticker"] for r in rows} == {"MSFT"}


def test_holdout_excluded_by_default(engine):
    with Session(engine) as s:
        _matured(s, "old", "A", OLD, finbert=0.5, car3=0.02)
        _matured(s, "recent", "B", RECENT, finbert=0.5, car3=0.02)
        s.commit()
        default = {r["ticker"] for r in load_lab_rows(s, now=NOW)}
        assert default == {"A"}  # recent (holdout) excluded
        with_holdout = {r["ticker"] for r in load_lab_rows(s, now=NOW, include_holdout=True)}
        assert with_holdout == {"A", "B"}


def test_backfill_excluded_by_default(engine):
    with Session(engine) as s:
        _matured(s, "live", "A", OLD, finbert=0.5, car3=0.02)
        _matured(s, "bf", "B", OLD, finbert=0.5, car3=0.02, backfill=True)
        s.commit()
        default = {r["ticker"] for r in load_lab_rows(s, now=NOW)}
        assert default == {"A"}  # backfill excluded from headline
        with_bf = {r["ticker"] for r in load_lab_rows(s, now=NOW, include_backfill=True)}
        assert with_bf == {"A", "B"}


def test_lab_api_endpoints(engine):
    from fastapi.testclient import TestClient

    from pipeline.api import create_app

    with Session(engine) as s:
        for i in range(6):
            _matured(s, f"m{i}", f"T{i}", OLD, finbert=i / 6, car3=i / 100)
        # An open (unmatured) observation for /lab/observations/open.
        _matured(s, "open1", "OPEN", OLD, finbert=0.2, car3=0.0)
        s.get(SignalObservation, "open1").status = "open"
        s.get(SignalObservation, "open1").marks_json = {"car_1d": 0.005}
        s.commit()

    client = TestClient(create_app(engine))
    ic = client.get("/lab/ic").json()
    assert ic["n"] == 6  # 6 matured, holdout/backfill defaults applied
    ic3 = next(x for x in ic["ic"] if x["horizon"] == 3)
    assert ic3["ic"] == 1.0  # monotonic finbert->car3

    open_obs = client.get("/lab/observations/open").json()
    assert open_obs["count"] == 1
    assert open_obs["items"][0]["running_car"] == {"car_1d": 0.005}
