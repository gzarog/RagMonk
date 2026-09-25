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


class LocalStorageModeRequiredError(RagMonkError):
    """Raised when code that only makes sense for local per-project sqlite
    (``AppContext.project_conn()`` and its established local-mode-only
    siblings ``code.graph.all_project_connections``/``conn_for_source_path``)
    is reached while ``storage.mode`` is anything other than ``"local"``.

    Storage backend abstraction plan, Phase 6 established the rule this
    exception now enforces uniformly: never silently fall back to local
    SQLite in server mode. ``exit_code = EXIT_CONFIG_ERROR`` because the
    root cause is always a storage-mode/call-site mismatch, not bad user
    input or a genuine database fault.
    """

    exit_code = EXIT_CONFIG_ERROR


class ContentChangedDuringProcessingError(RagMonkError):
    """Raised by a processor (indexing optimization plan, Phase P3) when
    a file's content changed between the coordinator's scan and this
    processor finishing its extraction -- a narrow but real race (a
    concurrent write landing mid-conversion). Never left to reach a CLI
    exit code: ``IndexCoordinator._process_queue`` catches every
    processor exception per file and retries/backs off exactly as it
    does for any other failure, which is the correct response here too
    -- publishing what was just extracted would silently commit content
    derived from a file that no longer looks like that on disk.
    """
