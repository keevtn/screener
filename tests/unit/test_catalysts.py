"""Gate 3 task 3.2: rules-based catalyst classifier over configs/catalysts.yaml."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline.enrich.canonical import from_values as canonical
from pipeline.score.catalysts import CatalystTaxonomy, classify_catalyst, load_taxonomy

BASE = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)


def _item(title, description="", filing_type=None):
    extra = {"filing_type": filing_type} if filing_type else {}
    src = "SEC EDGAR — filing" if filing_type else "Reuters"
    return canonical(
        id="c1",
        source=src,
        source_class="structured",
        title=title,
        description=description,
        published_at=BASE,
        extra=extra,
    )


@pytest.fixture(scope="module")
def tax():
    return load_taxonomy()


@pytest.mark.parametrize(
    ("title", "desc", "filing_type", "expected_type"),
    [
        (
            "Apple enters merger agreement",
            "Item 1.01 Entry into a Material Definitive Agreement",
            "8-K",
            "ma",
        ),
        ("BigCo to acquire SmallCo for $5B", "", None, "ma"),
        ("Company X exploring strategic alternatives", "", None, "ma"),
        ("Acme reports third quarter results", "", "10-Q", "earnings_results"),
        ("Acme reports Q3 earnings per share of $1.20", "", None, "earnings_results"),
        ("Widget Inc raises full-year guidance", "", None, "guidance_change"),
        ("Widget Inc cuts guidance amid soft demand", "", None, "guidance_change"),
        ("Startup prices public offering of 10M shares", "", "424B5", "secondary_offering"),
        ("Investor discloses activist stake, nominates directors", "", "SC 13D", "activist_stake"),
        ("FDA approves new therapy for XYZ", "", None, "fda_action"),
        ("Company receives complete response letter from FDA", "", None, "fda_action"),
        ("NewCo initial public offering begins trading", "", "S-1", "ipo"),
        ("Trading halted pending news", "", None, "halt"),
        ("Bureau of Economic Analysis releases GDP by state", "", None, None),
    ],
)
def test_event_classifier_table(tax, title, desc, filing_type, expected_type):
    result = tax.classify(_item(title, desc, filing_type))
    got = result.catalyst_type if result else None
    assert got == expected_type


def test_event_stage(tax):
    rumor = tax.classify(_item("Company X exploring strategic alternatives"))
    assert (rumor.catalyst_type, rumor.event_stage) == ("ma", "rumor")
    announced = tax.classify(
        _item("Merger agreement signed", "Item 1.01 material agreement", "8-K")
    )
    assert (announced.catalyst_type, announced.event_stage) == ("ma", "announced")


def test_direction_from_keyword_subset(tax):
    assert tax.classify(_item("Widget raises full-year guidance")).direction_hint == "bullish"
    assert tax.classify(_item("Widget cuts guidance")).direction_hint == "bearish"
    assert (
        tax.classify(_item("Startup prices public offering", filing_type="S-3")).direction_hint
        == "bearish"
    )


def test_ipo_calendar_only_non_predictive(tax):
    result = tax.classify(_item("NewCo IPO begins trading", filing_type="S-1"))
    assert result.catalyst_type == "ipo"
    assert result.predictive is False  # calendar-only, never reaches the signal engine


def test_s1_high_materiality_high_alert(tax):
    # The founding two-axis example: S-1 tone is ~neutral but materiality is high.
    result = tax.classify(_item("NewCo files for initial public offering", filing_type="S-1"))
    assert result.materiality >= 0.7
    assert result.high_alert is True


def test_earnings_marked_reaction_dependent(tax):
    result = tax.classify(_item("Acme reports third quarter results", filing_type="10-Q"))
    assert result.reaction_dependent is True
    assert result.direction_hint == "reaction_dependent"


def test_taxonomy_zero_code_new_type(tax):
    # A brand-new catalyst type added only in config is detected with no code change (I11).
    spec = {
        "high_alert_cutoff": 0.7,
        "catalysts": {
            "spinoff": {
                "default_materiality": 0.65,
                "direction_hint": "none",
                "predictive": True,
                "detection": {"keywords": ["to spin off", "spinoff", "spin-off"]},
            }
        },
    }
    custom = CatalystTaxonomy(spec)
    result = custom.classify(_item("Conglomerate to spin off its media division"))
    assert result.catalyst_type == "spinoff"
    assert result.materiality == 0.65


def test_no_catalyst_returns_none():
    assert classify_catalyst(_item("Weekly market wrap: stocks mixed")) is None


def test_keywords_match_headline_not_body(tax):
    # A real catalyst leads the headline; an incidental M&A verb in the body does not.
    assert tax.classify(_item("Want to buy a house before 2026?")) is None
    assert (
        tax.classify(
            _item("New Foreign Direct Investment data", "firms spent billions to acquire assets")
        )
        is None
    )
    # But a headline-led acquisition still classifies as M&A.
    assert (
        tax.classify(_item("NU-Med Plus announces acquisition of Avid Gold")).catalyst_type == "ma"
    )
