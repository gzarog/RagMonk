from __future__ import annotations

from datetime import UTC, datetime

from ragmonk.indexing.retry import (
    MAX_ATTEMPTS,
    MAX_DELAY_SECONDS,
    backoff_seconds,
    is_permanent,
    next_attempt_at,
)


def test_backoff_grows_exponentially() -> None:
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(4) == 16.0


def test_backoff_is_capped_at_max_delay() -> None:
    assert backoff_seconds(100) == MAX_DELAY_SECONDS


def test_backoff_treats_sub_one_attempt_as_first_attempt() -> None:
    assert backoff_seconds(0) == backoff_seconds(1)


def test_next_attempt_at_is_in_the_future() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    result = datetime.fromisoformat(next_attempt_at(1, now=now))
    assert result > now


def test_is_permanent_before_and_after_threshold() -> None:
    assert is_permanent(MAX_ATTEMPTS - 1) is False
    assert is_permanent(MAX_ATTEMPTS) is True
