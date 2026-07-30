"""Defensive numeric coercion for API serialization.

SQLite is dynamically typed: a column declared REAL can still physically hold a
TEXT value (e.g. a legacy row, or a positional table copy that shifted a
timestamp into a numeric column — see the fundamentals price/change_pct incident).
SQLAlchemy's Float type does not re-coerce on read for pysqlite, so such a value
reaches the API as a Python ``str`` and serializes as a JSON string, which then
blows up a frontend ``value.toFixed(...)``.

``to_float`` is the choke point: it returns a real ``float`` for anything
genuinely numeric (including a numeric string like ``"123.45"``) and ``None`` for
anything else (a timestamp string, empty, non-finite). API endpoints run every
numeric field through it so the contract is always "a real number or null" — one
bad row can never emit a value that crashes a consumer.
"""

from __future__ import annotations

import math
from typing import Any


def to_float(v: Any) -> float | None:
    """A finite float, or None. Recovers numeric strings; rejects non-numeric
    strings, bools, NaN/inf, and anything else."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None
