"""TRADER view builders: round-trip pairing, account/portfolio shaping, the
read-only paper reader's paper-endpoint guardrail, and DB provenance joins.

No network: the reader takes an injected http; the DB tests use the real SQLite
`session` fixture from conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline.api import trader
from pipeline.common.models import (
    Cluster,
    ClusterScore,
    RawItem,
    SimConfig,
    SimTrade,
)
from pipeline.marketdata.alpaca import PAPER_URL
from pipeline.marketdata.paper_account import (
    PaperAccountReader,
    PaperAccountReaderError,
    paper_reader,
)


# --------------------------------------------------------------------------- #
# reader guardrails
# --------------------------------------------------------------------------- #
@pytest.fixture
def _keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")


def test_reader_refuses_non_paper_endpoint(_keys):
    with pytest.raises(PaperAccountReaderError):
        PaperAccountReader(base_url="https://api.alpaca.markets")


def test_reader_none_without_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert paper_reader() is None


def test_reader_has_no_write_methods():
    """Structural guarantee: no order-placement verb exists on the reader."""
    for verb in ("post", "delete", "submit", "submit_market", "cancel", "flatten_all"):
        assert not hasattr(PaperAccountReader, verb)


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Http:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls += 1
        assert url.startswith(PAPER_URL)
        return _Resp(self.body)


def test_reader_ttl_cache_collapses_calls(_keys):
    http = _Http({"is_open": True})
    reader = PaperAccountReader(http=http, ttl=60)
    reader.clock()
    reader.clock()
    assert http.calls == 1  # second call served from cache


def test_reader_ttl_zero_disables_cache(_keys):
    http = _Http({"is_open": True})
    reader = PaperAccountReader(http=http, ttl=0)
    reader.clock()
    reader.clock()
    assert http.calls == 2


# --------------------------------------------------------------------------- #
# account / portfolio shaping
# --------------------------------------------------------------------------- #
def test_account_view_day_pl():
    acct = {
        "account_number": "PA1234567890",
        "status": "ACTIVE",
        "equity": "10500",
        "last_equity": "10000",
        "cash": "5000",
        "buying_power": "20000",
        "portfolio_value": "10500",
    }
    v = trader.account_view(acct, {"is_open": True, "next_close": "x"})
    assert v["configured"] is True
    assert v["day_pl"] == 500.0
    assert v["day_pl_pct"] == pytest.approx(0.05)
    assert v["account_mask"] == "••••7890"
    assert v["clock"]["is_open"] is True


def test_portfolio_history_skips_null_equity():
    hist = {
        "timestamp": [1, 2, 3],
        "equity": [None, 10000, 10100],
        "profit_loss": [None, 0, 100],
        "profit_loss_pct": [None, 0, 0.01],
        "base_value": 10000,
        "timeframe": "1D",
    }
    v = trader.portfolio_history_view(hist)
    assert len(v["points"]) == 2
    assert v["points"][0]["equity"] == 10000.0


# --------------------------------------------------------------------------- #
# round-trip pairing (pure)
# --------------------------------------------------------------------------- #
def _fill(sym, side, qty, price, t, oid):
    return {"symbol": sym, "side": side, "qty": qty, "price": price, "time": t, "order_id": oid}


def test_pair_simple_long_round_trip():
    fills = [
        _fill("AAPL", "buy", 10, 100.0, "2026-07-20T13:30:00Z", "o1"),
        _fill("AAPL", "sell", 10, 110.0, "2026-07-20T20:00:00Z", "o2"),
    ]
    trips = trader.pair_round_trips(fills)
    assert len(trips) == 1
    t = trips[0]
    assert t["direction"] == 1
    assert t["qty"] == 10
    assert t["realized_pl"] == 100.0  # (110-100)*10
    assert t["realized_pl_pct"] == pytest.approx(0.1)
    assert t["entry_order_id"] == "o1"
    assert t["exit_order_id"] == "o2"


def test_pair_short_round_trip():
    fills = [
        _fill("TSLA", "sell", 5, 200.0, "2026-07-20T13:30:00Z", "s1"),
        _fill("TSLA", "buy", 5, 190.0, "2026-07-20T19:00:00Z", "s2"),
    ]
    trips = trader.pair_round_trips(fills)
    assert len(trips) == 1
    t = trips[0]
    assert t["direction"] == -1
    assert t["realized_pl"] == 50.0  # short: (190-200)*5*-1 = +50


def test_pair_partial_fills_and_open_remainder():
    fills = [
        _fill("NVDA", "buy", 10, 100.0, "2026-07-20T13:30:00Z", "b1"),
        _fill("NVDA", "sell", 4, 105.0, "2026-07-20T14:00:00Z", "s1"),
        _fill("NVDA", "sell", 3, 110.0, "2026-07-20T15:00:00Z", "s2"),
    ]
    trips = trader.pair_round_trips(fills)
    # 4 + 3 closed against the 10-share lot; 3 remain open (not a round-trip).
    assert sum(t["qty"] for t in trips) == 7
    assert all(t["direction"] == 1 for t in trips)


def test_fills_from_orders_drops_unfilled():
    orders = [
        {"symbol": "AAPL", "side": "buy", "status": "filled", "filled_qty": "10",
         "filled_avg_price": "100", "id": "o1", "filled_at": "2026-07-20T13:30:00Z"},
        {"symbol": "AAPL", "side": "buy", "status": "canceled", "filled_qty": "0",
         "filled_avg_price": None, "id": "o2"},
    ]
    fills = trader.fills_from_orders(orders)
    assert len(fills) == 1
    assert fills[0]["order_id"] == "o1"


# --------------------------------------------------------------------------- #
# provenance joins (real SQLite session fixture from conftest)
# --------------------------------------------------------------------------- #
def _seed_provenance(session):
    now = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
    session.add(RawItem(
        id="raw1", source="Reuters", source_class="structured",
        url="https://ex.com/a", published_at=now, ingested_at=now,
        payload_json={"title": "AcmeCorp announces FDA approval"},
    ))
    session.add(Cluster(cluster_id="c1", origin_item_id="raw1", member_count=1, created_at=now))
    session.add(ClusterScore(cluster_id="c1", catalyst_type="fda", materiality=0.9,
                             high_alert=True, created_at=now))
    session.add(SimConfig(config_id="cfg1", name="fda-momentum", created_at=now,
                          params_json={}, enabled=True))
    session.add(SimTrade(
        trade_id="t1", config_id="cfg1", ticker="ACME", direction=1,
        entered_at=now, entry_price=100.0, entry_source="alpaca-paper",
        horizon_trading_days=3, features_json={"notional": 1000.0}, cluster_id="c1",
        status="open", created_at=now, broker="alpaca-paper",
        broker_entry_order_id="o1",
    ))
    session.commit()


def test_open_position_provenance(session):
    _seed_provenance(session)
    positions = [{
        "symbol": "ACME", "side": "long", "qty": "10", "avg_entry_price": "100",
        "current_price": "108", "market_value": "1080", "cost_basis": "1000",
        "unrealized_pl": "80", "unrealized_plpc": "0.08", "change_today": "0.02",
    }]
    v = trader.positions_view(positions, session)
    assert v["count"] == 1
    prov = v["items"][0]["provenance"]
    assert prov["config_name"] == "fda-momentum"
    assert prov["catalyst_type"] == "fda"
    assert prov["headline"] == "AcmeCorp announces FDA approval"
    assert prov["source_class"] == "structured"


def test_round_trip_provenance_join(session):
    _seed_provenance(session)
    trips = [{
        "ticker": "ACME", "direction": 1, "qty": 10,
        "entry_price": 100.0, "entry_time": "2026-07-20T13:30:00Z", "entry_order_id": "o1",
        "exit_price": 110.0, "exit_time": "2026-07-20T20:00:00Z", "exit_order_id": "o2",
        "realized_pl": 100.0, "realized_pl_pct": 0.1,
    }]
    enriched = trader.enrich_round_trips(trips, session)
    assert enriched[0]["provenance"]["config_name"] == "fda-momentum"
    assert enriched[0]["provenance"]["headline"] == "AcmeCorp announces FDA approval"


def test_round_trip_no_match_is_null(session):
    _seed_provenance(session)
    trips = [{
        "ticker": "ZZZ", "direction": 1, "qty": 1,
        "entry_price": 1.0, "entry_time": None, "entry_order_id": "unknown",
        "exit_price": 2.0, "exit_time": None, "exit_order_id": "unknown2",
        "realized_pl": 1.0, "realized_pl_pct": 1.0,
    }]
    enriched = trader.enrich_round_trips(trips, session)
    assert enriched[0]["provenance"] is None
