"""Google-Trends search-interest lane: client parse, snapshot, hot-set, own-baseline z."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    RawItem,
    SearchInterestDaily,
)
from pipeline.ingest.trends import (
    GoogleTrendsClient,
    hot_set,
    hourly_interest,
    own_z,
    parse_timeline,
    parse_timeline_hourly,
    search_interest_z,
    snapshot_search_interest,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _epoch(d: date) -> str:
    return str(int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()))


def _explore(has_timeseries=True) -> str:
    widgets = [{"id": "GEO_MAP", "token": "g", "request": {}}]
    if has_timeseries:
        widgets.insert(0, {"id": "TIMESERIES", "token": "tok", "request": {"time": "x"}})
    return ")]}',\n" + json.dumps({"widgets": widgets})


def _multiline(points: list[tuple[date, float]]) -> str:
    tl = [{"time": _epoch(d), "formattedTime": str(d), "value": [v]} for d, v in points]
    return ")]}',\n" + json.dumps({"default": {"timelineData": tl}})


def _multiline_hourly(points: list[tuple[datetime, float]]) -> str:
    tl = [{"time": str(int(dt.timestamp())), "value": [v]} for dt, v in points]
    return ")]}',\n" + json.dumps({"default": {"timelineData": tl}})


class FakeHourlyClient:
    """Stands in for GoogleTrendsClient.interest_hourly: scripted (status, series)."""

    def __init__(self, result):
        self._result = result  # (status, series) or a callable(term)
        self.calls = 0

    def interest_hourly(self, term, *, timeframe="now 7-d"):
        self.calls += 1
        if callable(self._result):
            return self._result(term)
        return self._result


class FakeTrendsHttp:
    def __init__(self, explore_text, multiline_text, *, explore_status=200, multi_status=200):
        self._e, self._m = explore_text, multiline_text
        self._es, self._ms = explore_status, multi_status
        self.calls = []

    def get(self, url, params):
        self.calls.append(url)
        if "explore" in url:
            return self._es, (self._e if self._es == 200 else "")
        return self._ms, (self._m if self._ms == 200 else "")


class FakeClient:
    """Stands in for GoogleTrendsClient: returns a scripted (status, series)."""

    def __init__(self, result, *, fixed_series=None):
        self._result = result  # (status, series) or a callable(term)
        self._fixed = fixed_series
        self.calls = []

    def interest_series(self, term):
        self.calls.append(term)
        if callable(self._result):
            return self._result(term)
        if self._fixed is not None:
            return 200, self._fixed
        return self._result


# --- pure parse --------------------------------------------------------------


def test_parse_timeline():
    payload = json.loads(_multiline([(date(2026, 7, 18), 10.0), (date(2026, 7, 19), 42.0)])[5:])
    pts = parse_timeline(payload)
    assert pts == [(date(2026, 7, 18), 10.0), (date(2026, 7, 19), 42.0)]
    assert parse_timeline({}) == []


def test_client_interest_series():
    http = FakeTrendsHttp(_explore(), _multiline([(date(2026, 7, 19), 55.0)]))
    client = GoogleTrendsClient(http=http)
    status, series = client.interest_series("AAPL stock")
    assert status == 200 and series == [(date(2026, 7, 19), 55.0)]
    assert http.calls[0].endswith("/explore") and "multiline" in http.calls[1]


def test_client_explore_rate_limited():
    http = FakeTrendsHttp(_explore(), _multiline([]), explore_status=429)
    status, series = GoogleTrendsClient(http=http).interest_series("AAPL stock")
    assert status == 429 and series is None
    assert len(http.calls) == 1  # never reaches multiline


def test_client_no_timeseries_widget():
    http = FakeTrendsHttp(_explore(has_timeseries=False), _multiline([]))
    status, series = GoogleTrendsClient(http=http).interest_series("AAPL stock")
    assert status == "no-timeseries" and series is None


# --- hourly parse + client + on-demand cache ---------------------------------


def test_parse_timeline_hourly():
    h0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    h1 = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    payload = json.loads(_multiline_hourly([(h0, 5.0), (h1, 20.0)])[5:])
    assert parse_timeline_hourly(payload) == [(h0, 5.0), (h1, 20.0)]
    assert parse_timeline_hourly({}) == []


def test_client_interest_hourly():
    h1 = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    http = FakeTrendsHttp(_explore(), _multiline_hourly([(h1, 33.0)]))
    status, series = GoogleTrendsClient(http=http).interest_hourly("AAPL stock")
    assert status == 200 and series == [(h1, 33.0)]
    assert http.calls[0].endswith("/explore") and "multiline" in http.calls[1]


def test_hourly_interest_cached_sliced_and_ttl():
    series = [(datetime(2026, 7, 20, h, 0, tzinfo=UTC), float(h)) for h in range(6)]
    client = FakeHourlyClient((200, series))
    cache: dict = {}
    r = hourly_interest("aapl", client=client, hours=3, cache=cache, now=100.0)
    assert r["ticker"] == "AAPL" and r["source"] == "google_trends"
    assert r["label"] == "relative interest (0-100, own-term)"
    assert len(r["points"]) == 3  # tail slice of the last `hours`
    assert r["points"][-1] == {"hour": series[-1][0].isoformat(), "value": 5.0}
    assert client.calls == 1
    # within TTL -> cache hit (no second network call)
    hourly_interest("AAPL", client=client, hours=3, cache=cache, now=200.0)
    assert client.calls == 1
    # after TTL -> refetch
    hourly_interest("AAPL", client=client, hours=3, cache=cache, now=100.0 + 1800.0 + 1)
    assert client.calls == 2


def test_hourly_interest_fail_soft_unavailable():
    r = hourly_interest("TSLA", client=FakeHourlyClient((429, None)), cache={}, now=0.0)
    assert r["source"] == "unavailable" and r["points"] == [] and "429" in r["note"]

    def boom(term):
        raise RuntimeError("down")

    r2 = hourly_interest("TSLA", client=FakeHourlyClient(boom), cache={}, now=0.0)
    assert r2["source"] == "unavailable" and r2["points"] == []


def test_own_z_pure():
    assert own_z([10.0] * 12, 10.0) is None  # zero variance -> None
    assert own_z([1.0, 2.0, 3.0], 50.0, min_days=10) is None  # too few days
    z = own_z([10, 12, 8, 11, 9, 10, 13, 7, 10, 11, 9, 12], 40.0)
    assert z is not None and z > 5


# --- snapshot ----------------------------------------------------------------


def test_snapshot_backfills_series_and_upserts(engine):
    series = [(date(2026, 7, 18), 10.0), (date(2026, 7, 19), 20.0), (date(2026, 7, 20), 30.0)]
    with Session(engine) as s:
        client = FakeClient((200, series))
        stats = snapshot_search_interest(
            s, ["AAPL", "TSLA"], client, now=NOW, sleep=lambda _s: None
        )
        assert stats["tickers"] == 2 and stats["rows"] == 6  # full series backfilled per ticker
        rows = s.execute(select(SearchInterestDaily).where(SearchInterestDaily.ticker == "AAPL")).scalars().all()
        assert len(rows) == 3 and rows[0].source == "google_trends" and rows[0].term == "AAPL stock"

        # Re-run upserts (no duplicate rows), refreshing values.
        client2 = FakeClient((200, [(date(2026, 7, 20), 99.0)]))
        snapshot_search_interest(s, ["AAPL"], client2, now=NOW, sleep=lambda _s: None)
        n = s.execute(select(func.count()).select_from(SearchInterestDaily)
                      .where(SearchInterestDaily.ticker == "AAPL")).scalar_one()
        assert n == 3  # still 3 (upsert), and 7/20 refreshed
        v = s.get(SearchInterestDaily, ("AAPL", date(2026, 7, 20))).interest
        assert v == 99.0


def test_snapshot_stops_on_consecutive_429(engine):
    with Session(engine) as s:
        client = FakeClient((429, None))
        stats = snapshot_search_interest(
            s, ["A", "B", "C", "D", "E"], client, now=NOW, sleep=lambda _s: None,
            max_consecutive_429=3,
        )
        assert stats["tickers"] == 0 and stats["rate_limited"] == 3  # stopped early
        assert len(client.calls) == 3  # didn't hammer all 5


def test_snapshot_fail_soft_on_exception(engine):
    def boom(term):
        raise RuntimeError("network down")

    with Session(engine) as s:
        stats = snapshot_search_interest(
            s, ["AAPL"], FakeClient(boom), now=NOW, sleep=lambda _s: None
        )
        assert stats["failed"] == 1 and stats["tickers"] == 0  # logged, no crash


# --- hot set -----------------------------------------------------------------


def _seed_catalyst(s, cid, ticker, materiality, published):
    s.add(RawItem(id=cid, source="Reuters", source_class="structured", url=f"https://x/{cid}",
                  published_at=published, ingested_at=published,
                  payload_json={"title": cid, "guid": cid}))
    s.flush()
    s.add(Cluster(cluster_id=cid, origin_item_id=cid, member_ids_json=[cid], origin_tier=1,
                  member_count=1, created_at=published))
    s.add(ClusterScore(cluster_id=cid, finbert_score=0.5, lm_score=0.1, text_kind="article",
                       catalyst_type="ma", materiality=materiality, high_alert=True,
                       predictive=True, reaction_dependent=False, created_at=published))
    s.add(ClusterEntity(cluster_id=cid, ticker=ticker, ticker_role="subject",
                        match_method="name", created_at=published))


def test_hot_set_watchlist_first_then_catalysts_capped(engine):
    from pipeline.common.timeutil import utcnow

    recent = utcnow() - timedelta(hours=2)
    with Session(engine) as s:
        _seed_catalyst(s, "c1", "NVDA", 0.9, recent)
        _seed_catalyst(s, "c2", "MOS", 0.3, recent)
        _seed_catalyst(s, "old", "OLD", 0.99, utcnow() - timedelta(days=10))  # stale -> excluded
        s.commit()
        picks = hot_set(s, limit=3, days=3, watchlist=["SPY", "QQQ"])
        assert picks[:2] == ["SPY", "QQQ"]  # watchlist first
        assert "NVDA" in picks and "OLD" not in picks  # recent catalyst in, stale out
        assert len(picks) == 3  # capped


# --- own-baseline z ----------------------------------------------------------


def test_search_interest_z_own_baseline(engine):
    hist = [10, 12, 8, 11, 9, 10, 13, 7, 10, 11, 9, 12]  # 12 days, mean ~10.2, std ~1.7
    with Session(engine) as s:
        for i, v in enumerate(hist):
            d = date(2026, 7, 8) + timedelta(days=i)
            s.add(SearchInterestDaily(ticker="AAPL", date=d, interest=float(v),
                                      term="AAPL stock", updated_at=NOW))
        s.add(SearchInterestDaily(ticker="AAPL", date=date(2026, 7, 20), interest=40.0,
                                  term="AAPL stock", updated_at=NOW))  # today: a spike
        s.commit()
        z = search_interest_z(s, "AAPL", today=date(2026, 7, 20))
        assert z is not None and z > 5  # clearly anomalous vs its own history
        # Too little history -> None (needs the baseline clock to run).
        assert search_interest_z(s, "AAPL", today=date(2026, 7, 20), min_days=99) is None
        assert search_interest_z(s, "UNKNOWN", today=date(2026, 7, 20)) is None
