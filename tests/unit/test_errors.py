from __future__ import annotations

from ragmonk.core.errors import (
    ConfigError,
    DatabaseError,
    HealthCheckError,
    IndexingPartialFailureError,
    RagMonkError,
    SecurityViolationError,
    SourceUnavailableError,
    UsageError,
)


def test_default_exit_code_is_generic_failure() -> None:
    assert RagMonkError("boom").exit_code == 1


def test_each_error_class_maps_to_its_documented_exit_code() -> None:
    assert UsageError("x").exit_code == 2
    assert ConfigError("x").exit_code == 3
    assert SourceUnavailableError("x").exit_code == 4
    assert DatabaseError("x").exit_code == 5
    assert IndexingPartialFailureError("x").exit_code == 6
    assert HealthCheckError("x").exit_code == 7
    assert SecurityViolationError("x").exit_code == 8


def test_exit_code_can_be_overridden_explicitly() -> None:
    assert RagMonkError("x", exit_code=42).exit_code == 42
