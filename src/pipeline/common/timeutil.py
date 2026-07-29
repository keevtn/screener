"""Time helpers. The ONLY module allowed to call datetime.now() (docs/ROADMAP.md section 6)."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time, tz-aware UTC."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Normalize an aware datetime to UTC; reject naive ones (I1)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: all timestamps must be tz-aware (I1)")
    return dt.astimezone(UTC)
