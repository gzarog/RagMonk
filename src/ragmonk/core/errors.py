"""Exception hierarchy mapped onto RagMonk's stable CLI exit codes."""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_GENERIC_FAILURE = 1
EXIT_INVALID_ARGUMENTS = 2
EXIT_CONFIG_ERROR = 3
EXIT_SOURCE_UNAVAILABLE = 4
EXIT_DATABASE_ERROR = 5
EXIT_INDEXING_PARTIAL_FAILURE = 6
EXIT_HEALTH_CHECK_FAILURE = 7
EXIT_SECURITY_RESTRICTION = 8


class RagMonkError(Exception):
    """Base class for all RagMonk errors that carry a CLI exit code."""

    exit_code: int = EXIT_GENERIC_FAILURE

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(RagMonkError):
    exit_code = EXIT_INVALID_ARGUMENTS


class ConfigError(RagMonkError):
    exit_code = EXIT_CONFIG_ERROR


class SourceUnavailableError(RagMonkError):
    exit_code = EXIT_SOURCE_UNAVAILABLE


class DatabaseError(RagMonkError):
    exit_code = EXIT_DATABASE_ERROR


class IndexingPartialFailureError(RagMonkError):
    exit_code = EXIT_INDEXING_PARTIAL_FAILURE


class HealthCheckError(RagMonkError):
    exit_code = EXIT_HEALTH_CHECK_FAILURE


class SecurityViolationError(RagMonkError):
    exit_code = EXIT_SECURITY_RESTRICTION
