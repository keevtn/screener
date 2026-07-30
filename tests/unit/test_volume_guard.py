"""Persistent-volume guard: the decision matrix + the driver's hard refusal on an
ephemeral (deploy-cutover) container."""

from __future__ import annotations

import logging
import os

from pipeline.common import volume


def _clear_railway(monkeypatch):
    for k in volume._RAILWAY_MARKERS:
        monkeypatch.delenv(k, raising=False)


def _set_railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")


def _both_false(monkeypatch):
    # neither positive proof holds (control the two signals directly, so the tests
    # are platform-independent — no reliance on real st_dev / os.access)
    monkeypatch.setattr(volume, "is_persistent_mount", lambda p: False)
    monkeypatch.setattr(volume, "mount_path_confirms", lambda p: False)


# --- sqlite_dir parsing -----------------------------------------------------
def test_sqlite_dir_absolute_and_relative():
    # basename is platform-agnostic (abspath prepends a drive on Windows; on
    # Railway/Linux the 4-slash form resolves to /data exactly).
    assert os.path.basename(volume.sqlite_dir("sqlite:////data/pipeline.db")) == "data"
    assert os.path.basename(volume.sqlite_dir("sqlite:///data/pipeline.db")) == "data"
    assert volume.sqlite_dir("postgresql://x/y") is None  # non-sqlite
    assert volume.sqlite_dir("sqlite:///:memory:") is None


# --- mount_path_confirms (the primary Railway proof) ------------------------
def test_mount_path_confirms_under_and_writable(monkeypatch):
    # use os.sep so the under-path check matches on any platform (prod is Linux)
    mount = os.sep + "data"
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", mount)
    monkeypatch.setattr(os, "access", lambda p, m: True)  # writable
    monkeypatch.setattr(os.path, "realpath", lambda p: p)  # identity for the test
    assert volume.mount_path_confirms(mount) is True                       # dir == mount
    assert volume.mount_path_confirms(mount + os.sep + "sub") is True      # under mount
    assert volume.mount_path_confirms(os.sep + "other") is False           # not under mount


def test_mount_path_confirms_requires_env_and_writable(monkeypatch):
    mount = os.sep + "data"
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    assert volume.mount_path_confirms(mount) is False           # no env -> not confirmed
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", mount)
    monkeypatch.setattr(os, "access", lambda p, m: False)       # not writable
    assert volume.mount_path_confirms(mount) is False


# --- decision matrix (persistence x on_railway) -----------------------------
def test_not_on_railway_always_ok(monkeypatch):
    _clear_railway(monkeypatch)
    _both_false(monkeypatch)
    st = volume.volume_status("sqlite:////data/pipeline.db")
    assert st["ok"] is True and st["on_railway"] is False


def test_railway_stdev_confirms(monkeypatch):
    # secondary proof: DB on a separate device from / (mount-path proof absent)
    _set_railway(monkeypatch)
    monkeypatch.setattr(volume, "is_persistent_mount", lambda p: True)
    monkeypatch.setattr(volume, "mount_path_confirms", lambda p: False)
    st = volume.volume_status("sqlite:////data/pipeline.db")
    assert st["ok"] is True and st["confirmed"] is True


def test_railway_mount_path_confirms_despite_same_stdev(monkeypatch):
    # THE FALSE-REFUSAL REGRESSION: a real Railway volume that shares the root
    # device (st_dev == /) but has RAILWAY_VOLUME_MOUNT_PATH set with the DB under
    # it must now be CONFIRMED, not refused. This is what took the driver down.
    _set_railway(monkeypatch)
    monkeypatch.setattr(volume, "is_persistent_mount", lambda p: False)  # same device
    monkeypatch.setattr(volume, "mount_path_confirms", lambda p: True)   # mount-path proof
    st = volume.volume_status("sqlite:////data/pipeline.db")
    assert st["ok"] is True
    assert st["mount_confirmed"] is True and st["persistent"] is False


def test_railway_true_ephemeral_refused(monkeypatch):
    # true cutover: neither proof holds -> still refuse (fail-closed preserved)
    _set_railway(monkeypatch)
    _both_false(monkeypatch)
    st = volume.volume_status("sqlite:////data/pipeline.db")
    assert st["ok"] is False
    assert "EPHEMERAL" in st["reason"]


def test_non_sqlite_backend_ok(monkeypatch):
    _set_railway(monkeypatch)
    st = volume.volume_status("postgresql://u:p@h/db")
    assert st["ok"] is True and st["db_dir"] is None


def test_require_persistent_volume_logs_and_returns(monkeypatch, caplog):
    _set_railway(monkeypatch)
    _both_false(monkeypatch)
    log = logging.getLogger("test.volume")
    with caplog.at_level(logging.ERROR):
        ok = volume.require_persistent_volume(log, "TRADER driver", "sqlite:////data/pipeline.db")
    assert ok is False
    assert any("EPHEMERAL CONTAINER" in r.message or "EPHEMERAL" in r.getMessage() for r in caplog.records)


# --- the driver hard-refuses before touching Alpaca -------------------------
def test_driver_refuses_on_ephemeral_before_alpaca(monkeypatch):
    """run_trader_driver must return None on an ephemeral container without
    constructing any Alpaca client (the guard is checked first)."""
    from pipeline.sim import driver

    # force the guard to fail regardless of the real filesystem
    monkeypatch.setattr(
        "pipeline.common.volume.require_persistent_volume", lambda *a, **k: False
    )
    # if the guard didn't short-circuit, constructing AlpacaData/broker without keys
    # would raise — returning None cleanly proves the guard ran first.
    result = driver.run_trader_driver(engine=object())  # engine never touched
    assert result is None
