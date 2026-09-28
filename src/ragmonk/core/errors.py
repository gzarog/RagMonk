"""Exception hierarchy mapped onto RagMonk's stable CLI exit codes."""

from __future__ import annotations

from pathlib import Path

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


class RunLockTimeoutError(RagMonkError):
    """Raised when a runtime lock (``index.lock`` etc.) could not be
    acquired within its bounded timeout. Carries the (diagnostic-only,
    untrusted) owner metadata read from the lock file; the OS file lock
    itself remains the sole authority on who holds it.
    """

    def __init__(
        self,
        lock_path: str,
        timeout_seconds: float,
        *,
        owner: dict[str, object] | None = None,
    ) -> None:
        owner = owner or {}
        self.lock_path = lock_path
        self.timeout_seconds = timeout_seconds
        self.owner_pid = owner.get("pid")
        self.owner_operation = owner.get("operation")
        self.owner_source_id = owner.get("source_id")
        self.owner_acquired_at = owner.get("acquired_at")
        self.owner_hostname = owner.get("hostname")
        name = Path(lock_path).name
        if owner:
            details = [f"PID {self.owner_pid}"] if self.owner_pid is not None else []
            if self.owner_operation:
                details.append(f"operation={self.owner_operation}")
            if self.owner_source_id:
                details.append(f"source={self.owner_source_id}")
            if self.owner_hostname:
                details.append(f"host={self.owner_hostname}")
            message = (
                f"Another RagMonk process holds {name} ({', '.join(details) or 'unknown owner'}); "
                f"timed out after {timeout_seconds:g}s waiting for it."
            )
        else:
            message = (
                f"Another RagMonk process holds {name} (owner unknown); "
                f"timed out after {timeout_seconds:g}s waiting for it."
            )
        super().__init__(message)


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
