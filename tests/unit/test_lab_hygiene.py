"""Lab hygiene: artifact guards + per-event dedup (the XAIR lesson, encoded)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from pipeline.common.models import SignalObservation
from pipeline.lab.analysis import dedupe_per_event, marks_suspect
from pipeline.lab.marking import series_suspect


def test_marks_suspect_flags_split_glitch():
    # XAIR-shaped: -7.8% at h1 then +1,783% at h3 — an adjacent-horizon step of ~18x.
    assert marks_suspect({"car_1d": -0.078, "car_3d": 17.83}) is True
    # explicit marking-time flag wins regardless of values
    assert marks_suspect({"car_1d": 0.01, "suspect_series": True}) is True
    # a violent-but-real move stays in
    assert marks_suspect({"car_1d": 0.35, "car_3d": 0.80, "car_5d": 1.1}) is False
    assert marks_suspect({}) is False
    assert marks_suspect(None) is False
    # first observed horizon already absurd
    assert marks_suspect({"car_3d": 5.0}) is True


def test_series_suspect_detects_single_day_jump():
    d = date
    ok = {d(2026, 7, 1): 10.0, d(2026, 7, 2): 11.0, d(2026, 7, 3): 9.5}
    assert series_suspect(ok, d(2026, 7, 1)) is False
    broken = {d(2026, 7, 1): 1.0, d(2026, 7, 2): 19.0, d(2026, 7, 3): 19.5}  # 19x = split break
    assert series_suspect(broken, d(2026, 7, 1)) is True
    collapse = {d(2026, 7, 1): 20.0, d(2026, 7, 2): 1.0}  # 20x down
    assert series_suspect(collapse, d(2026, 7, 1)) is True
    # jumps BEFORE entry don't matter (entry slice only)
    assert series_suspect(broken, d(2026, 7, 3)) is False


def _obs(ticker, t0, materiality):
    return SignalObservation(
        observation_id=f"{ticker}-{t0.isoformat()}-{materiality}",
        cluster_id="c",
        ticker=ticker,
        t0=t0,
        features_json={"materiality": materiality},
        status="matured",
        created_at=t0,
    )


def test_dedupe_per_event_keeps_max_materiality():
    day = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    obs = [
        _obs("XAIR", day, 0.6),
        _obs("XAIR", day.replace(hour=13), 0.9),  # same event day, higher materiality
        _obs("XAIR", day.replace(hour=14), 0.9),  # tie -> earlier t0 wins
        _obs("MSFT", day, 0.5),
        _obs("XAIR", datetime(2026, 7, 17, 9, 0, tzinfo=UTC), 0.4),  # different day survives
    ]
    out = dedupe_per_event(obs)
    assert len(out) == 3
    xair_16 = next(o for o in out if o.ticker == "XAIR" and o.t0.date().day == 16)
    assert xair_16.features_json["materiality"] == 0.9 and xair_16.t0.hour == 13
