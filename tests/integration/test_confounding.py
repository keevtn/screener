"""Gate 5c task 5c.3: clean_window confounding control (novelty_rank covered in 5c.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, RawItem, SignalObservation
from pipeline.lab.marking import mark_observation

BD = pd.bdate_range("2025-02-24", "2025-04-11")
SPY = pd.DataFrame({"date": BD, "adj_close": [500.0] * len(BD)})
TICK = pd.DataFrame({"date": BD, "adj_close": [100.0 + (i % 3) for i in range(len(BD))]})


class FakeProvider:
    benchmark = "SPY"

    def get_benchmark_bars(self, start, end):
        return SPY

    def get_daily_bars(self, ticker, start, end):
        return TICK


def _obs(session, oid, t0, materiality):
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
    obs = SignalObservation(
        observation_id=oid,
        cluster_id=oid,
        ticker="T",
        t0=t0,
        features_json={"materiality": materiality},
        marks_json={},
        backfill=False,
        status="open",
        created_at=t0,
    )
    session.add(obs)
    session.commit()
    return obs


A_T0 = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # entry 3/12
B_T0 = datetime(2025, 3, 14, 18, 0, tzinfo=UTC)  # +2 trading days


def test_clean_window_flips_on_nearby_material_cluster(engine):
    provider = FakeProvider()
    with Session(engine) as s:
        a = _obs(s, "A", A_T0, materiality=0.6)
        _obs(s, "B", B_T0, materiality=0.9)  # material, +2 trading days
        mark_observation(s, a, provider)
        assert a.status == "matured"
        assert a.clean_window is False  # contaminated by B within ±3 trading days


def test_clean_window_true_when_isolated(engine):
    provider = FakeProvider()
    with Session(engine) as s:
        a = _obs(s, "A", A_T0, materiality=0.6)
        _obs(s, "B", B_T0, materiality=0.1)  # immaterial -> does not contaminate
        mark_observation(s, a, provider)
        assert a.clean_window is True
