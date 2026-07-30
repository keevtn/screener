"""Standing paper-trading driver: heartbeat + double-driver signal, boot
reconciliation, and the intraday session (sweep -> flatten + flatten_all
backstop). All offline — fake broker/reader/quote, injected clock + sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from pipeline.common.models import SimConfig, SimTrade, TraderHeartbeat
from pipeline.sim import driver


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeBroker:
    def __init__(self):
        self.flatten_calls = 0
        self.submitted = []

    def flatten_all(self):
        self.flatten_calls += 1
        return 0

    # run_sim_cycle would call these only if there were enabled configs / open
    # trades; the driver tests keep the ledger empty so they're never hit.
    def open_position_count(self):
        return 0

    def submit_market(self, *a, **k):  # pragma: no cover - not reached in these tests
        raise AssertionError("no orders expected in driver session tests")


class FakeReader:
    def __init__(self, positions=None, orders=None):
        self._positions = positions or []
        self._orders = orders or []

    def positions(self):
        return self._positions

    def orders(self, **_kw):
        return self._orders


def _factory(engine):
    return lambda: Session(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# heartbeat + double-driver signal
# --------------------------------------------------------------------------- #
def test_heartbeat_write_read_roundtrip(session):
    t0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    conflict = driver.write_heartbeat(
        session, driver_id="hostA:1:100", host="hostA", pid=1, started_at=t0,
        now=t0, sweeps=3, note="sweep", session_date="2026-07-20", stale_after_s=180.0,
    )
    assert conflict is False
    hb = driver.read_heartbeat(session, now=t0 + timedelta(seconds=5), stale_after_s=180.0)
    assert hb["present"] and hb["alive"] is True
    assert hb["driver_id"] == "hostA:1:100"
    assert hb["sweeps"] == 3


def test_heartbeat_same_driver_no_conflict(session):
    t0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    driver.write_heartbeat(session, driver_id="A", host="h", pid=1, started_at=t0,
                           now=t0, sweeps=0, note="sweep", session_date=None, stale_after_s=180.0)
    c = driver.write_heartbeat(session, driver_id="A", host="h", pid=1, started_at=t0,
                               now=t0 + timedelta(seconds=60), sweeps=1, note="sweep",
                               session_date=None, stale_after_s=180.0)
    assert c is False


def test_heartbeat_second_live_driver_flags_conflict(session):
    t0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    driver.write_heartbeat(session, driver_id="A", host="hA", pid=1, started_at=t0,
                           now=t0, sweeps=0, note="sweep", session_date=None, stale_after_s=180.0)
    # a DIFFERENT driver beats 30s later (within the stale window) -> conflict
    c = driver.write_heartbeat(session, driver_id="B", host="hB", pid=2, started_at=t0,
                               now=t0 + timedelta(seconds=30), sweeps=0, note="sweep",
                               session_date=None, stale_after_s=180.0)
    assert c is True
    hb = driver.read_heartbeat(session, now=t0 + timedelta(seconds=31), stale_after_s=180.0)
    assert hb["conflict"] is True


def test_heartbeat_stale_previous_driver_no_conflict(session):
    t0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    driver.write_heartbeat(session, driver_id="A", host="hA", pid=1, started_at=t0,
                           now=t0, sweeps=0, note="sweep", session_date=None, stale_after_s=180.0)
    # driver B beats long after A went stale (> 180s) -> A is dead, no conflict
    c = driver.write_heartbeat(session, driver_id="B", host="hB", pid=2, started_at=t0,
                               now=t0 + timedelta(seconds=600), sweeps=0, note="sweep",
                               session_date=None, stale_after_s=180.0)
    assert c is False


# --------------------------------------------------------------------------- #
# boot reconciliation
# --------------------------------------------------------------------------- #
def test_reconcile_orphans_and_missing(session):
    now = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    session.add(SimConfig(config_id="cfg1", name="c", created_at=now, params_json={}, enabled=True))
    session.flush()
    for tk in ("AAPL", "MSFT"):
        session.add(SimTrade(
            config_id="cfg1", ticker=tk, direction=1, entered_at=now, entry_price=10.0,
            entry_source="alpaca-paper", horizon_trading_days=0, features_json={},
            status="open", created_at=now,
        ))
    session.commit()
    # Alpaca holds AAPL (matches) + ZZZ (orphan); MSFT is open in DB but not on Alpaca.
    reader = FakeReader(positions=[{"symbol": "AAPL"}, {"symbol": "ZZZ"}], orders=[])
    summary = driver.reconcile_on_boot(session, reader, now=now)
    assert summary["orphans"] == ["ZZZ"]
    assert summary["missing"] == ["MSFT"]
    assert summary["db_open_trades"] == 2


# --------------------------------------------------------------------------- #
# intraday session
# --------------------------------------------------------------------------- #
def test_session_flattens_when_past_cutoff(engine):
    now = datetime(2026, 7, 20, 19, 55, tzinfo=UTC)  # past a 19:50 flatten
    broker = FakeBroker()
    reader = FakeReader()
    clock = {"timestamp": now, "is_open": True, "next_close": now + timedelta(minutes=5)}
    sweeps = driver.run_session(
        now - timedelta(minutes=5), clock,  # flatten_at in the past -> force now
        session_factory=_factory(engine), reader=reader, broker=broker,
        quote=lambda t: 100.0, driver_id="A", host="h", pid=1, started_at=now,
        sleep=lambda s: None, now_fn=lambda: now, sweep_interval_s=1.0,
    )
    assert sweeps == 1
    assert broker.flatten_calls == 1  # backstop ran
    with Session(engine) as s:
        hb = driver.read_heartbeat(s, now=now, stale_after_s=180.0)
        assert hb["note"] == "flatten"


def test_session_sweeps_then_stops_at_max(engine):
    now = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    broker = FakeBroker()
    clock = {"timestamp": now, "is_open": True}
    sweeps = driver.run_session(
        now + timedelta(hours=1), clock,  # flatten well in the future
        session_factory=_factory(engine), reader=FakeReader(), broker=broker,
        quote=lambda t: 100.0, driver_id="A", host="h", pid=1, started_at=now,
        sleep=lambda s: None, now_fn=lambda: now, sweep_interval_s=1.0, max_sweeps=3,
    )
    assert sweeps == 3
    assert broker.flatten_calls == 0  # never forced -> no backstop


def test_session_refuses_without_flatten_cutoff(engine):
    now = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    broker = FakeBroker()
    sweeps = driver.run_session(
        None, {"timestamp": now},  # no flatten cutoff -> refuse to trade
        session_factory=_factory(engine), reader=FakeReader(), broker=broker,
        quote=lambda t: 100.0, driver_id="A", host="h", pid=1, started_at=now,
        sleep=lambda s: None, now_fn=lambda: now,
    )
    assert sweeps == 0
    assert broker.flatten_calls == 0


def test_driver_enabled_flag(monkeypatch):
    monkeypatch.delenv("TRADER_DRIVER_ENABLED", raising=False)
    assert driver.driver_enabled() is False
    monkeypatch.setenv("TRADER_DRIVER_ENABLED", "true")
    assert driver.driver_enabled() is True
    monkeypatch.setenv("TRADER_DRIVER_ENABLED", "0")
    assert driver.driver_enabled() is False
