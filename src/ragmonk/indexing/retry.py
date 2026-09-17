"""Exponential backoff for transient indexing failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 300.0
MAX_ATTEMPTS = 5


def backoff_seconds(attempt: int) -> float:
    """``attempt`` is the 1-based number of attempts made so far."""
    if attempt < 1:
        attempt = 1
    return min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)


def next_attempt_at(attempt: int, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return (now + timedelta(seconds=backoff_seconds(attempt))).isoformat()


def is_permanent(attempt: int) -> bool:
    return attempt >= MAX_ATTEMPTS
