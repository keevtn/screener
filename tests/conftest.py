"""Shared fixtures: a real SQLite engine in tmpdir (roadmap section 6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

# Make the repo root importable so tests can exercise scripts/ (namespace pkg).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from pipeline.common.db import make_engine  # noqa: E402
from pipeline.common.models import Base  # noqa: E402
from pipeline.marketdata import BAR_COLUMNS, MarketDataProvider  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def make_provider(tmp_path):
    """Factory: build a MarketDataProvider backed by in-memory synthetic bars.

    Pass ``{ticker: DataFrame}`` where each frame has at least ``date`` and
    ``adj_close`` columns; missing OHLCV columns are filled. Each call gets an
    isolated cache dir so repeated builds in one test don't collide.
    """
    counter = {"n": 0}

    def _make(frames_by_ticker):
        counter["n"] += 1
        cache = tmp_path / f"bars{counter['n']}"
        norm: dict[str, pd.DataFrame] = {}
        for ticker, df in frames_by_ticker.items():
            d = df.copy()
            for col in ("open", "high", "low", "volume"):
                if col not in d.columns:
                    d[col] = 0
            norm[ticker.upper()] = d

        def downloader(ticker, start, end):
            return norm.get(ticker.upper(), pd.DataFrame(columns=BAR_COLUMNS))

        return MarketDataProvider(cache, downloader=downloader)

    return _make
