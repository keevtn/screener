"""Gate 2 task 2.4: entity resolution passes, blocklist, fuzzy, M&A roles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline.enrich.canonical import from_values as canonical
from pipeline.enrich.resolve import EntityResolver, resolve_cluster

ENTITIES = [
    {"ticker": "AAPL", "canonical_name": "Apple Inc.", "aliases_json": ["Apple"]},
    {
        "ticker": "META",
        "canonical_name": "Meta Platforms, Inc.",
        "aliases_json": ["Meta", "Meta Platforms", "Facebook"],
    },
    {"ticker": "GOOGL", "canonical_name": "Alphabet Inc.", "aliases_json": ["Alphabet", "Google"]},
    {"ticker": "MSFT", "canonical_name": "Microsoft Corp", "aliases_json": ["Microsoft"]},
    {
        "ticker": "ATVI",
        "canonical_name": "Activision Blizzard, Inc.",
        "aliases_json": ["Activision", "Activision Blizzard"],
    },
]


@pytest.fixture
def resolver():
    return EntityResolver(ENTITIES)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Meta Platforms unveils new Quest headset", ["META"]),  # alias
        ("Apple beats on iPhone demand", ["AAPL"]),  # alias
        ("Alphabet reorganizes search division", ["GOOGL"]),  # alias-file entry
        ("$AAPL soars after hours", ["AAPL"]),  # cashtag
    ],
)
def test_resolution_positive_cases(resolver, text, expected):
    assert sorted(resolver.resolve(text).tickers) == sorted(expected)


def test_cashtag_blocklist_unmapped(resolver):
    result = resolver.resolve("$A is a great buy right now")
    assert result.tickers == []
    assert [(u.mention, u.reason) for u in result.unmapped] == [("$A", "blocklist")]


def test_unknown_cashtag_below_fuzzy_threshold_unmapped(resolver):
    result = resolver.resolve("$ZZZZ mystery ticker of the day")
    assert result.tickers == []
    assert ("$ZZZZ", "no_match") in [(u.mention, u.reason) for u in result.unmapped]


def test_ma_role_attribution(resolver):
    result = resolver.resolve("Microsoft to acquire Activision Blizzard for $69B")
    roles = {m.ticker: m.role for m in result.matches}
    assert roles == {"MSFT": "acquirer", "ATVI": "target"}


def test_single_company_defaults_subject(resolver):
    result = resolver.resolve("Apple reports record Q3 earnings")
    roles = {m.ticker: m.role for m in result.matches}
    assert roles == {"AAPL": "subject"}


TGT = {"ticker": "TGT", "canonical_name": "Target Corporation", "aliases_json": ["Target"]}


@pytest.fixture
def resolver_tgt():
    return EntityResolver([*ENTITIES, TGT])


def test_common_word_name_guarded(resolver_tgt):
    # "target" in "price target" must NOT attribute to Target Corp (TGT).
    result = resolver_tgt.resolve("Analyst raises price target on Apple")
    assert "TGT" not in result.tickers
    assert "AAPL" in result.tickers  # distinctive single-token name still resolves
    assert ("target", "common_word") in [(u.mention, u.reason) for u in result.unmapped]


def test_common_word_corroborated_by_cashtag(resolver_tgt):
    # A cashtag corroborates the common word -> TGT is a legitimate attribution.
    result = resolver_tgt.resolve("$TGT Target raises full-year guidance")
    assert result.tickers == ["TGT"]
    assert not any(u.reason == "common_word" for u in result.unmapped)


def test_distinctive_multi_token_name_bypasses_guard(resolver_tgt):
    # The full "Target Corporation" is distinctive (multi-token) -> resolves.
    assert "TGT" in resolver_tgt.resolve("Target Corporation raises guidance").tickers


def test_resolve_cluster_pulls_body_cashtags(resolver):
    item = canonical(
        id="c1",
        source="Reuters",
        source_class="structured",
        title="Big tech earnings roundup",
        description="Watching $MSFT and $AAPL closely this week.",
        published_at=datetime(2025, 3, 12, 14, tzinfo=UTC),
    )
    assert sorted(resolve_cluster(item, resolver).tickers) == ["AAPL", "MSFT"]


def test_cashtag_tickers_fast_filter(resolver):
    # Real-universe cashtags matched; blocklist/common-word and non-universe dropped.
    assert resolver.cashtag_tickers("loading up on $AAPL and $META today") == ["AAPL", "META"]
    assert resolver.cashtag_tickers("no tickers here, just vibes") == []
    assert resolver.cashtag_tickers("$AAPL $AAPL $aapl") == ["AAPL"]  # distinct, case-insensitive
    # $YOLO isn't a real symbol -> dropped (no fuzzy in the fast path, unlike resolve()).
    assert resolver.cashtag_tickers("$YOLO to the moon") == []


def test_cashtag_tickers_respects_blocklist():
    # A blocklisted cashtag (common word) is never emitted even if it were a ticker.
    r = EntityResolver(ENTITIES + [{"ticker": "A", "canonical_name": "Agilent", "aliases_json": []}],
                       blocklist={"A"})
    assert r.cashtag_tickers("$A is blocklisted but $AAPL is not") == ["AAPL"]
