"""Phase-2 paper BROKER: guardrails, fill reconciliation, engine execution path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    RawItem,
    SimConfig,
    SimTrade,
)
from pipeline.sim.broker import (
    AlpacaPaperBroker,
    BrokerGuardrailError,
    PAPER_URL,
    ensure_broker_columns,
)
from pipeline.sim.engine import evaluate_entries, evaluate_exits

NOW = datetime(2026, 7, 17, 14, 0, tzinfo=UTC)  # ~10:00 ET, RTH

KEYS = {"ALPACA_API_KEY": "PKTEST", "ALPACA_API_SECRET": "secretsecret"}


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Scripts Alpaca order lifecycle: POST /orders -> id; GET /orders/{id}
    walks through a status script so reconcile() polls to a terminal state."""

    def __init__(self, *, account=None, fill_price=101.25, order_status_script=None, positions=0):
        self._account = account or {
            "account_number": "PA123", "status": "ACTIVE", "buying_power": "400000",
            "cash": "100000", "equity": "100000", "trading_blocked": False,
        }
        self._fill_price = fill_price
        self._script = order_status_script or ["new", "filled"]
        self._poll = 0
        self._positions = positions
        self.submitted: list[dict] = []
        self._next_id = 1000

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/v2/account"):
            return _Resp(self._account)
        if url.endswith("/v2/positions"):
            return _Resp([{"symbol": f"X{i}", "qty": "10", "side": "long"}
                          for i in range(self._positions)])
        if "/v2/orders/" in url:
            status = self._script[min(self._poll, len(self._script) - 1)]
            self._poll += 1
            last = self.submitted[-1]
            filled = status == "filled"
            return _Resp({
                "id": last["id"], "symbol": last["symbol"], "side": last["side"],
                "qty": last["qty"], "status": status,
                "filled_qty": last["qty"] if filled else "0",
                "filled_avg_price": str(self._fill_price) if filled else None,
                "submitted_at": "2026-07-17T14:00:00Z",
            })
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        assert url.endswith("/v2/orders")
        assert json["type"] == "market" and json["time_in_force"] == "day"  # guardrails
        oid = f"ord-{self._next_id}"
        self._next_id += 1
        self.submitted.append({**json, "id": oid})
        return _Resp({"id": oid, **json})


def _broker(http, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", KEYS["ALPACA_API_KEY"])
    monkeypatch.setenv("ALPACA_API_SECRET", KEYS["ALPACA_API_SECRET"])
    # Fast reconcile defaults so engine-path tests don't sleep on the real cadence.
    return AlpacaPaperBroker(http=http, notional=1000.0,
                             reconcile_timeout_s=5.0, reconcile_poll_s=0.01)


# --- guardrails --------------------------------------------------------------


def test_refuses_non_paper_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKX")
    monkeypatch.setenv("ALPACA_API_SECRET", "sx")
    for live in ("https://api.alpaca.markets", "https://broker-api.alpaca.markets", "http://evil"):
        with pytest.raises(BrokerGuardrailError):
            AlpacaPaperBroker(http=FakeHttp(), base_url=live)


def test_assert_paper_ready_rejects_blocked(monkeypatch):
    http = FakeHttp(account={"status": "ACTIVE", "buying_power": "100", "trading_blocked": True})
    b = _broker(http, monkeypatch)
    with pytest.raises(BrokerGuardrailError):
        b.assert_paper_ready()


def test_assert_paper_ready_rejects_zero_buying_power(monkeypatch):
    http = FakeHttp(account={"status": "ACTIVE", "buying_power": "0", "trading_blocked": False})
    b = _broker(http, monkeypatch)
    with pytest.raises(BrokerGuardrailError):
        b.assert_paper_ready()


def test_account_reports_paper_endpoint(monkeypatch):
    b = _broker(FakeHttp(), monkeypatch)
    acct = b.assert_paper_ready()
    assert acct["endpoint"] == PAPER_URL and acct["buying_power"] == 400000.0


def test_per_run_order_cap(monkeypatch):
    b = _broker(FakeHttp(), monkeypatch)
    b.max_orders_per_run = 2
    b.submit_market("AAPL", 1, 5)
    b.submit_market("AAPL", 1, 5)
    with pytest.raises(BrokerGuardrailError):
        b.submit_market("AAPL", 1, 5)  # new entry refused at cap
    # A CLOSE order must still go through — a cap hit can never strand a position.
    oid = b.submit_market("AAPL", -1, 5, is_close=True)
    assert oid  # flatten always permitted


# --- fill reconciliation -----------------------------------------------------


def test_reconcile_returns_fill(monkeypatch):
    http = FakeHttp(fill_price=101.25, order_status_script=["new", "new", "filled"])
    b = _broker(http, monkeypatch)
    oid = b.submit_market("AAPL", 1, 9)
    fill = b.reconcile(oid, timeout_s=5, poll_s=0.01, sleep=lambda s: None)
    assert fill is not None
    assert fill.filled_avg_price == 101.25 and fill.filled_qty == 9.0 and fill.side == "buy"


def test_reconcile_none_on_rejected(monkeypatch):
    http = FakeHttp(order_status_script=["new", "rejected"])
    b = _broker(http, monkeypatch)
    oid = b.submit_market("THIN", -1, 3)
    assert b.reconcile(oid, timeout_s=5, poll_s=0.01, sleep=lambda s: None) is None


def test_reconcile_none_on_timeout(monkeypatch):
    http = FakeHttp(order_status_script=["new"])  # never terminal
    b = _broker(http, monkeypatch)
    oid = b.submit_market("SLOW", 1, 1)
    assert b.reconcile(oid, timeout_s=0.05, poll_s=0.01, sleep=lambda s: None) is None


# --- engine execution path ---------------------------------------------------


def _seed(s, cid, ticker, *, finbert=0.7, published=None):
    published = published or (NOW - timedelta(minutes=5))
    s.add(RawItem(id=cid, source="Reuters", source_class="structured", url=f"https://x/{cid}",
                  published_at=published, ingested_at=published,
                  payload_json={"title": f"{ticker} news", "guid": cid}))
    s.flush()
    s.add(Cluster(cluster_id=cid, origin_item_id=cid, member_ids_json=[cid], origin_tier=1,
                  member_count=1, created_at=published))
    s.add(ClusterScore(cluster_id=cid, finbert_score=finbert, lm_score=finbert,
                       text_kind="article", catalyst_type="ma", event_stage="announced",
                       materiality=0.8, high_alert=True, predictive=True,
                       reaction_dependent=False, created_at=published))
    s.add(ClusterEntity(cluster_id=cid, ticker=ticker, ticker_role="subject",
                        match_method="name", created_at=published))
    s.commit()


def _intraday_cfg(s):
    cfg = SimConfig(name="intraday-ma", created_at=NOW, enabled=True,
                    gate_ref="user-directive-exploratory-2026-07-17",
                    params_json={"high_alert_only": True, "direction": "finbert_sign",
                                 "direction_min_abs": 0.3, "horizon_trading_days": 0})
    s.add(cfg)
    s.commit()
    return cfg


def test_entry_uses_reconciled_fill_not_quote(engine, monkeypatch):
    with Session(engine) as s:
        _seed(s, "c1", "AAPL")
        _intraday_cfg(s)
        http = FakeHttp(fill_price=101.25)  # fill differs from the sizing quote (100)
        broker = _broker(http, monkeypatch)
        opened = evaluate_entries(s, lambda t: 100.0, now=NOW, broker=broker)
        assert len(opened) == 1
        t = opened[0]
        # entry_price is the RECONCILED FILL, never the quote used to size.
        assert t.entry_price == 101.25
        assert t.entry_source == "alpaca-paper" and t.broker == "alpaca-paper"
        assert t.broker_entry_order_id == "ord-1000"
        assert t.features_json["qty"] == int(1000 // 100)  # notional // sizing-quote = 10
        # A real order hit the paper endpoint with DAY/market guardrails.
        assert http.submitted[0]["side"] == "buy" and http.submitted[0]["time_in_force"] == "day"


def test_no_fill_writes_no_trade(engine, monkeypatch):
    with Session(engine) as s:
        _seed(s, "c1", "THIN")
        _intraday_cfg(s)
        # "rejected" is terminal on the first poll -> reconcile returns None with
        # no sleeping (status is checked before any sleep).
        http = FakeHttp(order_status_script=["rejected"])
        broker = _broker(http, monkeypatch)
        opened = evaluate_entries(s, lambda t: 50.0, now=NOW, broker=broker)
        assert opened == []  # rejected order -> no position (no-fill == no-trade)


def test_intraday_exit_before_close_with_broker(engine, monkeypatch):
    with Session(engine) as s:
        _seed(s, "c1", "AAPL")
        _intraday_cfg(s)
        http = FakeHttp(fill_price=100.0)
        broker = _broker(http, monkeypatch)
        evaluate_entries(s, lambda t: 100.0, now=NOW, broker=broker)  # enter ~10:00 ET

        # Before the 15:50-ET cutoff: no exit.
        assert evaluate_exits(s, lambda t: 110.0, now=NOW.replace(hour=17), broker=broker) == []
        # At/after the cutoff (19:50 UTC): a real closing order fills at 103.
        http._fill_price = 103.0
        http._poll = 0
        http._script = ["filled"]
        closed = evaluate_exits(s, lambda t: 110.0, now=NOW.replace(hour=20), broker=broker)
        assert len(closed) == 1
        t = closed[0]
        assert t.exit_reason == "close" and t.exit_price == 103.0
        assert t.broker_exit_order_id is not None
        # closing order was the OPPOSITE side of the long entry.
        assert http.submitted[-1]["side"] == "sell"


def test_ensure_broker_columns_idempotent(engine):
    # Columns exist from the model on a fresh engine; running the migration is a no-op.
    ensure_broker_columns(engine)
    ensure_broker_columns(engine)


def test_flatten_all_closes_positions(monkeypatch):
    # Two open positions -> flatten_all submits opposite-side closes and confirms.
    http = FakeHttp(positions=2)
    b = _broker(http, monkeypatch)
    closed = b.flatten_all()
    assert closed == 2
    # Both closing orders were is_close (didn't consume the entry cap).
    assert b._orders_placed == 0
    assert all(o["side"] == "sell" for o in http.submitted)  # closing a long -> sell
