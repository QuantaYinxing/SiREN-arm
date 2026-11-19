"""UTC ISO-8601 helpers. Timestamps stored in the DB and sent over the wire are strings
with a trailing 'Z'; tests inject a fixed clock via Store(now=...)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(later: str, earlier: str | None) -> float | None:
    """Seconds between two ISO timestamps; None when the earlier one is missing."""
    if not earlier:
        return None
    return (parse_iso(later) - parse_iso(earlier)).total_seconds()


def minutes_between(later: str, earlier: str | None) -> float:
    age = age_seconds(later, earlier)
    return 0.0 if age is None else age / 60.0


def date_of(iso: str) -> str:
    return parse_iso(iso).date().isoformat()


def add_minutes(iso: str, minutes: int | float) -> str:
    return now_iso(parse_iso(iso) + timedelta(minutes=float(minutes)))


def today(date: str | None = None) -> str:
    return date or now_iso().split("T")[0]
