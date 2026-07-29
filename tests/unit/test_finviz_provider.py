"""Task 0.6 gate tests: Finviz parsing, schema-drift + auth-expiry detection."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from pipeline.marketdata.finviz import (
    FINVIZ_EXPORT_URL,
    FinvizAuthError,
    FinvizProvider,
    FinvizSchemaError,
    parse_finviz_csv,
    parse_finviz_number,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "finviz_export_sample.csv"


def _csv() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_finviz_number_variants():
    assert parse_finviz_number("2.34B") == pytest.approx(2.34e9)
    assert parse_finviz_number("890.00M") == pytest.approx(8.90e8)
    assert parse_finviz_number("1,234,567") == pytest.approx(1234567.0)
    assert parse_finviz_number("5.23%") == pytest.approx(0.0523)
    assert parse_finviz_number("-") is None
    assert parse_finviz_number("") is None
    assert parse_finviz_number(None) is None


def test_parse_finviz_csv_happy_path():
    rows = {r.ticker: r for r in parse_finviz_csv(_csv())}
    assert set(rows) == {"BIGCAP", "SMALLCAP", "PENNY", "THIN", "FOREIGN"}
    big = rows["BIGCAP"]
    assert big.market_cap == pytest.approx(5.0e9)
    assert big.price == pytest.approx(120.0)
    assert big.short_float == pytest.approx(0.035)
    assert big.country == "USA"
    assert big.sector == "Technology"


def test_schema_drift_detected():
    # Rename a required column: parser must flag it, not silently null the field.
    drifted = _csv().replace('"Beta"', '"Beta Coefficient"')
    with pytest.raises(FinvizSchemaError, match="Beta"):
        parse_finviz_csv(drifted)


def test_auth_expiry_detected():
    html = "<!DOCTYPE html><html><body>Please log in</body></html>"
    with pytest.raises(FinvizAuthError):
        parse_finviz_csv(html)


def test_empty_token_rejected():
    with pytest.raises(FinvizAuthError):
        FinvizProvider("")


@respx.mock
def test_provider_fetch_over_http():
    respx.get(FINVIZ_EXPORT_URL).mock(return_value=httpx.Response(200, text=_csv()))
    rows = FinvizProvider("tok").fetch_fundamentals()
    assert {r.ticker for r in rows} == {"BIGCAP", "SMALLCAP", "PENNY", "THIN", "FOREIGN"}


@respx.mock
def test_provider_fetch_auth_expiry_over_http():
    respx.get(FINVIZ_EXPORT_URL).mock(return_value=httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(FinvizAuthError):
        FinvizProvider("expired").fetch_fundamentals()
