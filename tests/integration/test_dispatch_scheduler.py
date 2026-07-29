"""Gate 1 task 1.2: manual dispatch and the scheduled job produce identical rows."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import Base, RawItem
from pipeline.ingest import RawItemHandler
from pipeline.ingest.scheduler import DEFAULT_CADENCES, build_scheduler
from scripts.dispatch import run_source_once

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
RSS_CFG = [{"label": "Test Newswire", "url": "http://x", "source_type": "rss"}]


class FakeHttp:
    def __init__(self, text):
        self._text = text

    async def get_text_and_headers(self, url, headers=None):
        return self._text, {}

    async def get_text(self, url, headers=None):
        return self._text

    async def get_json(self, url, params=None):
        return {}


def _fresh_engine(tmp_path, name):
    eng = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(eng)
    return eng


def _row_ids(engine):
    with Session(engine) as s:
        return sorted(s.execute(select(RawItem.id)).scalars().all())


def test_default_cadences_are_sane():
    # EDGAR 1–5 min, RSS 5–15 min (roadmap 1.2).
    assert 60 <= DEFAULT_CADENCES["sec"] <= 300
    assert 300 <= DEFAULT_CADENCES["rss"] <= 900


def test_dispatch_equals_scheduler(tmp_path):
    rss_xml = (FEEDS / "sample_rss.xml").read_text()

    # Manual path.
    eng_manual = _fresh_engine(tmp_path, "manual.db")
    n_manual = asyncio.run(
        run_source_once("rss", RawItemHandler(eng_manual), http=FakeHttp(rss_xml), feeds=RSS_CFG)
    )

    # Scheduled path: the scheduler is wired to call the SAME run_source_once.
    eng_sched = _fresh_engine(tmp_path, "sched.db")
    sink_sched = RawItemHandler(eng_sched)

    async def dispatch_fn(source: str) -> int:
        return await run_source_once(source, sink_sched, http=FakeHttp(rss_xml), feeds=RSS_CFG)

    _sched, jobs = build_scheduler(dispatch_fn)
    rss_job = jobs["rss"]
    assert rss_job.trigger.interval.total_seconds() == DEFAULT_CADENCES["rss"]
    assert tuple(rss_job.args) == ("rss",)

    # Fire the scheduled job's target exactly as the scheduler would.
    n_sched = asyncio.run(rss_job.func(*rss_job.args))

    assert n_manual == n_sched == 2
    assert _row_ids(eng_manual) == _row_ids(eng_sched)
    assert len(_row_ids(eng_manual)) == 2


class FakeBskyHttp:
    """get_json returns a canned searchPosts response (the bluesky seam)."""

    def __init__(self, payload):
        self._payload = payload

    async def get_json(self, url, params=None):
        assert "app.bsky.feed.searchPosts" in url
        return self._payload


_BSKY_PAYLOAD = {
    "posts": [
        {
            "uri": "at://did:plc:abc/app.bsky.feed.post/3xyz",
            "author": {"handle": "trader.bsky.social"},
            "record": {
                "text": "$NVDA ripping premarket on the new GPU line",
                "createdAt": "2026-07-17T11:00:00Z",
            },
            "likeCount": 3,
            "replyCount": 1,
        },
        {
            "uri": "at://did:plc:def/app.bsky.feed.post/3abc",
            "author": {"handle": "macro.bsky.social"},
            "record": {"text": "CPI print tomorrow, watch bonds", "createdAt": "2026-07-17T11:05:00Z"},
        },
    ]
}


def test_dispatch_bluesky_archives_social(tmp_path):
    """Bluesky one-shot lands social raw_items (shadow-mode archive) and dedupes."""
    eng = _fresh_engine(tmp_path, "bsky.db")
    sink = RawItemHandler(eng)
    n = asyncio.run(
        run_source_once("bluesky", sink, http=FakeBskyHttp(_BSKY_PAYLOAD), feeds=["#stocks"])
    )
    assert n == 2

    with Session(eng) as s:
        rows = s.execute(select(RawItem)).scalars().all()
        assert {r.source for r in rows} == {"Bluesky"}
        assert {r.source_class for r in rows} == {"social"}  # I8 shadow lane
        by_text = {r.payload_json["description"]: r for r in rows}
        nvda = by_text["$NVDA ripping premarket on the new GPU line"]
        assert nvda.payload_json["extra"]["bsky_handle"] == "trader.bsky.social"
        assert nvda.payload_json["extra"]["search_term"] == "#stocks"
        assert nvda.url.startswith("https://bsky.app/profile/trader.bsky.social/post/")

    # Re-dispatch: DB unique id is the dedup authority -> zero new rows.
    n2 = asyncio.run(
        run_source_once("bluesky", sink, http=FakeBskyHttp(_BSKY_PAYLOAD), feeds=["#stocks"])
    )
    assert n2 == 0


# --- Reddit OAuth lane -------------------------------------------------------


class FakeRedditHttp:
    """Reddit-shaped injectable: mints an app-only token, serves a canned listing."""

    def __init__(self, listing):
        self._listing = listing
        self.token_calls = 0
        self.list_calls = 0

    async def post_form(self, url, data, auth, headers):
        self.token_calls += 1
        assert url.endswith("/api/v1/access_token")
        assert data["grant_type"] == "client_credentials"
        assert "User-Agent" in headers
        return 200, {"access_token": "tok123", "expires_in": 3600}

    async def get_json(self, url, headers, params):
        self.list_calls += 1
        assert headers["Authorization"] == "bearer tok123"
        assert "/new" in url
        return 200, self._listing


_REDDIT_LISTING = {
    "data": {
        "children": [
            {"kind": "t3", "data": {
                "title": "$NVDA to the moon", "selftext": "loading up on $NVDA",
                "subreddit": "wallstreetbets", "permalink": "/r/wallstreetbets/comments/abc/x/",
                "name": "t3_abc", "id": "abc", "created_utc": 1_752_000_000,
                "score": 42, "num_comments": 10}},
            {"kind": "t3", "data": {
                "title": "CPI print thoughts", "selftext": "",
                "subreddit": "economics", "permalink": "/r/economics/comments/def/y/",
                "name": "t3_def", "id": "def", "created_utc": 1_752_000_100}},
        ]
    }
}


def test_reddit_parse_listing_fields():
    from pipeline.ingest.reddit import parse_listing

    items = parse_listing(_REDDIT_LISTING)
    assert len(items) == 2
    nvda = items[0]
    assert nvda.source == "Reddit - wallstreetbets" and nvda.source_type == "social"
    assert nvda.title == "$NVDA to the moon"
    assert nvda.url == "https://www.reddit.com/r/wallstreetbets/comments/abc/x/"
    assert nvda.extra["guid"] == "t3_abc" and nvda.extra["subreddit"] == "wallstreetbets"
    assert nvda.published_at.tzinfo is not None  # UTC-aware from created_utc
    # empty selftext falls back to the title for description
    assert items[1].description == "CPI print thoughts"


def test_reddit_dispatch_writes_social_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "test-ua/0.1")
    eng = _fresh_engine(tmp_path, "reddit.db")
    sink = RawItemHandler(eng)
    http = FakeRedditHttp(_REDDIT_LISTING)
    # Two groups in ONE poll: the token is minted once and reused across both
    # group requests (the rate-budget property); both groups return the same two
    # posts, so the DB unique id keeps the total at 2 (intra-poll dedup).
    n = asyncio.run(run_source_once("reddit", sink, http=http, feeds=[["wallstreetbets"], ["economics"]]))
    assert n == 2 and http.token_calls == 1 and http.list_calls == 2
    with Session(eng) as s:
        rows = s.execute(select(RawItem)).scalars().all()
        assert {r.source_class for r in rows} == {"social"}  # I8 shadow lane
        assert any(r.source == "Reddit - wallstreetbets" for r in rows)
    # Re-run (fresh extractor -> fresh token): DB dedups -> zero new rows.
    n2 = asyncio.run(run_source_once("reddit", sink, http=http, feeds=[["wallstreetbets"]]))
    assert n2 == 0


_REDDIT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/trader</name></author>
    <category term="stocks" label="r/stocks"/>
    <content type="html">&lt;p&gt;$AAPL earnings discussion&lt;/p&gt;</content>
    <id>t3_abc</id>
    <link href="https://www.reddit.com/r/stocks/comments/abc/aapl/"/>
    <published>2026-07-19T12:00:00+00:00</published>
    <title>AAPL earnings thread</title>
  </entry>
  <entry>
    <category term="wallstreetbets" label="r/wallstreetbets"/>
    <id>t3_def</id>
    <link href="https://www.reddit.com/r/wallstreetbets/comments/def/yolo/"/>
    <published>2026-07-19T12:05:00+00:00</published>
    <title>YOLO update</title>
  </entry>
</feed>"""


class FakeRedditRSSHttp:
    def __init__(self, xml, status=200):
        self._xml, self._status, self.calls = xml, status, 0

    async def fetch(self, url, headers):
        self.calls += 1
        assert "/new/.rss" in url and "User-Agent" in headers
        return self._status, self._xml


def test_reddit_parse_rss_fields():
    from pipeline.ingest.reddit import parse_rss

    items = parse_rss(_REDDIT_RSS)
    assert len(items) == 2
    a = items[0]
    assert a.source == "Reddit - stocks" and a.source_type == "social"
    assert a.title == "AAPL earnings thread"
    assert a.url == "https://www.reddit.com/r/stocks/comments/abc/aapl/"
    assert a.extra["guid"] == "t3_abc" and a.extra["via"] == "rss"
    assert a.published_at.tzinfo is not None


def test_reddit_rss_fallback_writes_rows(tmp_path, monkeypatch):
    # No OAuth creds -> residential RSS fallback (the default that works today).
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    eng = _fresh_engine(tmp_path, "reddit_rss.db")
    sink = RawItemHandler(eng)
    http = FakeRedditRSSHttp(_REDDIT_RSS)
    n = asyncio.run(run_source_once("reddit", sink, http=http, feeds=[["stocks", "wallstreetbets"]]))
    assert n == 2 and http.calls == 1  # ONE group request per sweep (rate-limit safe)
    with Session(eng) as s:
        rows = s.execute(select(RawItem)).scalars().all()
        assert {r.source_class for r in rows} == {"social"}  # I8 shadow lane


def test_reddit_rss_rate_limited_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    eng = _fresh_engine(tmp_path, "reddit_429.db")
    # 429 -> skip this cycle, write nothing, never crash.
    n = asyncio.run(
        run_source_once("reddit", RawItemHandler(eng),
                        http=FakeRedditRSSHttp("", status=429), feeds=[["stocks"]])
    )
    assert n == 0
