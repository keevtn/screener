"""Gate 4 task 4.5: catalyst-armed drift — reaction beats text, no-bars, TTL expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import ArmedState, Cluster, Prediction, RawItem
from pipeline.signal.armed import arm_ticker, resolve_armed_state

# Wed 2025-03-12 17:00 ET (21:00 UTC) — after the close, so C0 is Thu 2025-03-13.
EVENT = datetime(2025, 3, 12, 21, 0, tzinfo=UTC)


def _bars(pairs):
    return pd.DataFrame(
        {"date": [pd.Timestamp(d) for d, _ in pairs], "adj_close": [float(c) for _, c in pairs]}
    )


class FakeProvider:
    benchmark = "SPY"

    def __init__(self, spy, tk):
        self._spy, self._tk = spy, tk

    def get_benchmark_bars(self, start, end):
        return self._spy

    def get_daily_bars(self, ticker, start, end):
        return self._spy if ticker == "SPY" else self._tk


def _seed_cluster(session, cid):
    session.add(
        RawItem(
            id=cid,
            source="SEC EDGAR — 10-Q",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=EVENT,
            ingested_at=EVENT,
            payload_json={"title": "earnings", "guid": cid},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=0,
            member_count=1,
            created_at=EVENT,
        )
    )
    session.commit()


def test_arm_ticker_idempotent(engine):
    with Session(engine) as s:
        _seed_cluster(s, "cl-earn")
        a = arm_ticker(s, "AAPL", "cl-earn", "earnings_results", EVENT, now=EVENT)
        b = arm_ticker(s, "AAPL", "cl-earn", "earnings_results", EVENT, now=EVENT)
        assert a.id == b.id
        assert s.execute(select(func.count()).select_from(ArmedState)).scalar_one() == 1


def test_armed_drift_direction(engine):
    # Earnings was bearish-toned, but the first-session reaction is +3% market-adjusted
    # (ticker +4%, SPY +1% from Wed close to Thu close) -> emitted direction is BULLISH.
    spy = _bars(
        [("2025-03-11", 499), ("2025-03-12", 500), ("2025-03-13", 505), ("2025-03-14", 506)]
    )
    tk = _bars([("2025-03-11", 99), ("2025-03-12", 100), ("2025-03-13", 104), ("2025-03-14", 103)])
    provider = FakeProvider(spy, tk)
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed_cluster(s, "cl-earn")
        armed = arm_ticker(s, "AAPL", "cl-earn", "earnings_results", EVENT, now=EVENT)
        pred = resolve_armed_state(
            s, armed, provider, cfg.params_json, cfg.config_version, now=EVENT + timedelta(days=1)
        )
        assert pred is not None
        assert pred.direction == "bullish"  # reaction beats the bearish text
        assert pred.evidence_json["reaction"] == 0.03
        assert armed.status == "resolved" and armed.resolution == "emitted"


def test_armed_no_bars_then_ttl_expiry(engine):
    # No post-event close available yet (SPY bars end on the event date).
    spy = _bars([("2025-03-10", 498), ("2025-03-11", 499), ("2025-03-12", 500)])
    tk = _bars([("2025-03-10", 99), ("2025-03-11", 99.5), ("2025-03-12", 100)])
    provider = FakeProvider(spy, tk)
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed_cluster(s, "cl-earn2")
        armed = arm_ticker(s, "AAPL", "cl-earn2", "earnings_results", EVENT, now=EVENT)

        # Within TTL, no bar yet -> emits nothing, stays armed.
        assert (
            resolve_armed_state(s, armed, provider, cfg.params_json, cfg.config_version, now=EVENT)
            is None
        )
        assert armed.status == "armed"
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 0

        # Past TTL, still no bar -> expires unresolved, no prediction.
        ttl_h = cfg.params_json["armed"]["ttl_hours"]
        later = EVENT + timedelta(hours=ttl_h + 1)
        assert (
            resolve_armed_state(s, armed, provider, cfg.params_json, cfg.config_version, now=later)
            is None
        )
        assert armed.status == "expired" and armed.resolution == "ttl_no_bars"
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 0
