"""Gate 5c task 5c.2: marking — entry no-lookahead, CAR, vol-scaling, idempotent."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, RawItem, SignalObservation
from pipeline.lab.marking import mark_observation, mark_observations

BD = pd.bdate_range("2025-02-24", "2025-04-04")


def _frame(base, overrides):
    closes = [overrides.get(d.strftime("%Y-%m-%d"), base) for d in BD]
    return pd.DataFrame({"date": BD, "adj_close": [float(c) for c in closes]})


class FakeProvider:
    benchmark = "SPY"

    def __init__(self, frames):
        self._frames = frames

    def get_benchmark_bars(self, start, end):
        return self._frames["SPY"]

    def get_daily_bars(self, ticker, start, end):
        return self._frames[ticker]


def _seed_obs(session, oid, ticker, t0):
    session.add(
        RawItem(
            id=oid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{oid}",
            published_at=t0,
            ingested_at=t0,
            payload_json={"title": oid, "guid": oid},
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
        ticker=ticker,
        t0=t0,
        features_json={},
        marks_json={},
        backfill=False,
        status="open",
        created_at=t0,
    )
    session.add(obs)
    session.commit()
    return obs


# SPY flat at 500; TICK entry 100 with known forward closes.
_SPY = _frame(500, {})
_TICK = _frame(100, {"2025-03-10": 99, "2025-03-11": 101, "2025-03-13": 102, "2025-03-14": 103})


def test_entry_price_lookahead(engine):
    # After-hours t0 (Wed 17:00 ET) -> entry is the NEXT session's close, never same day (I12).
    t0 = datetime(2025, 3, 12, 21, 0, tzinfo=UTC)
    provider = FakeProvider({"SPY": _SPY, "TICK": _TICK})
    with Session(engine) as s:
        obs = _seed_obs(s, "o1", "TICK", t0)
        mark_observation(s, obs, provider)
        assert obs.entry_price_date == date(2025, 3, 13)  # Thu, not Wed 3/12


def test_car_hand_computed(engine):
    # During-hours t0 (Wed 14:00 ET) -> entry = Wed 3/12 close (100). SPY flat.
    t0 = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)
    provider = FakeProvider({"SPY": _SPY, "TICK": _TICK})
    with Session(engine) as s:
        obs = _seed_obs(s, "o1", "TICK", t0)
        matured = mark_observation(s, obs, provider)
        assert obs.entry_price_date == date(2025, 3, 12)
        # SPY flat -> CAR = ticker return. 3/13=102 -> +2%; 3/14=103 -> +3%.
        assert obs.marks_json["car_1d"] == 0.02
        assert obs.marks_json["car_2d"] == 0.03
        assert matured is True and obs.status == "matured"


def test_vol_scaling(engine):
    # Same forward CAR, different trailing vol -> different vol-scaled marks.
    t0 = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)
    low_vol = _frame(100, {"2025-03-10": 100.1, "2025-03-11": 99.9, "2025-03-13": 102})  # tiny vol
    high_vol = _frame(
        100,
        {
            "2025-03-05": 80,
            "2025-03-06": 120,
            "2025-03-10": 85,
            "2025-03-11": 115,
            "2025-03-13": 102,
        },
    )  # swingy pre-entry
    with Session(engine) as s:
        lo = _seed_obs(s, "lo", "LOW", t0)
        hi = _seed_obs(s, "hi", "HIGH", t0)
        provider = FakeProvider({"SPY": _SPY, "LOW": low_vol, "HIGH": high_vol})
        mark_observation(s, lo, provider)
        mark_observation(s, hi, provider)
        # Identical raw CAR...
        assert lo.marks_json["car_1d"] == hi.marks_json["car_1d"] == 0.02
        # ...but low trailing vol -> larger scaled mark.
        assert lo.marks_json["car_1d_volscaled"] > hi.marks_json["car_1d_volscaled"]


def test_marking_idempotent(engine):
    t0 = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)
    provider = FakeProvider({"SPY": _SPY, "TICK": _TICK})
    with Session(engine) as s:
        _seed_obs(s, "o1", "TICK", t0)
        marked, matured = mark_observations(s, provider)
        assert (marked, matured) == (1, 1)
        first_marks = dict(s.get(SignalObservation, "o1").marks_json)

        # Re-run: the matured observation is not re-marked.
        marked2, matured2 = mark_observations(s, provider)
        assert (marked2, matured2) == (0, 0)  # nothing open to mark
        assert s.get(SignalObservation, "o1").marks_json == first_marks


def _open_count(s):
    return len(
        s.execute(select(SignalObservation).where(SignalObservation.status == "open"))
        .scalars()
        .all()
    )


def test_mark_budget_caps_per_pass(engine):
    # A backlog of 5 maturable observations; max_marks=2 processes at most 2 per
    # pass so a sweep can't be monopolized. Random slice -> no fixed stuck-front.
    provider = FakeProvider({"SPY": _SPY, "TICK": _TICK})
    ts = [datetime(2025, 3, d, 18, 0, tzinfo=UTC) for d in (10, 11, 12, 13, 14)]
    with Session(engine) as s:
        for i, t0 in enumerate(ts, 1):
            _seed_obs(s, f"o{i}", "TICK", t0)

        marked, matured = mark_observations(s, provider, max_marks=2)
        assert marked <= 2 and matured <= 2  # capped, not all 5 at once
        assert _open_count(s) >= 3  # at most 2 left the open set this pass


def test_mark_budget_drains_over_passes_no_starvation(engine):
    # Repeated bounded passes eventually process EVERY observation (random
    # selection guarantees no permanent starvation), and each pass respects cap.
    provider = FakeProvider({"SPY": _SPY, "TICK": _TICK})
    ts = [datetime(2025, 3, d, 18, 0, tzinfo=UTC) for d in (10, 11, 12, 13, 14)]
    with Session(engine) as s:
        for i, t0 in enumerate(ts, 1):
            _seed_obs(s, f"o{i}", "TICK", t0)
        passes = 0
        while _open_count(s) > 0 and passes < 50:
            m, _ = mark_observations(s, provider, max_marks=2)
            assert m <= 2  # cap holds every pass
            passes += 1
        assert _open_count(s) == 0  # all matured — nothing starved
        assert passes >= 3  # 5 obs / 2-per-pass needed multiple passes
