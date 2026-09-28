"""Application context: config, DB connections, and a run lock, with a
graceful shutdown hook so every command starts and ends in the same way.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig, load_config
from ragmonk.core.errors import LocalStorageModeRequiredError, RunLockTimeoutError
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect
from ragmonk.telemetry.logging import configure_logging, get_logger, log_event
from ragmonk.update import background as update_background
from ragmonk.update import notifier as update_notifier

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised only on non-Windows platforms
    msvcrt = None  # type: ignore[assignment]


DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
_METADATA_MAX_BYTES = 4096
_METADATA_SCHEMA_VERSION = 1
# The OS lock covers byte 0 only (msvcrt locks are mandatory, so metadata
# lives after it and stays readable by a blocked process); byte 0 is padding.
_METADATA_OFFSET = 1
_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:\-]")

_logger = get_logger("lock")


def _sanitize_token(value: object, limit: int = 64) -> str | None:
    """Metadata is diagnostic and untrusted: keep only a short identifier-like
    token (never a URL/argv/secret-bearing string).
    """
    if value is None:
        return None
    return _SAFE_TOKEN.sub("_", str(value))[:limit] or None


@dataclass(frozen=True)
class LockStatus:
    """Result of a non-blocking inspection: ``state`` is free/held/unknown."""

    state: str
    owner: dict[str, Any] | None = None


def _try_lock(handle: Any) -> bool:
    """One non-blocking OS lock attempt; False if held by someone else."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _open_lock_file(path: Path) -> Any:
    # Never truncate on open: another process may own the lock and its
    # metadata must stay readable until we actually win the lock.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd, "r+b")


def read_lock_owner(path: Path) -> dict[str, Any] | None:
    """Defensively reads owner metadata; None if absent/malformed."""
    try:
        with path.open("rb") as fh:
            fh.seek(_METADATA_OFFSET)
            raw = fh.read(_METADATA_MAX_BYTES)
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    owner: dict[str, Any] = {}
    pid = data.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        owner["pid"] = pid
    for key in ("operation", "source_id", "hostname"):
        token = _sanitize_token(data.get(key))
        if token:
            owner[key] = token
    acquired_at = _sanitize_token(data.get("acquired_at"), 40)
    if acquired_at:
        owner["acquired_at"] = acquired_at
    return owner or None


def inspect_lock(path: Path) -> LockStatus:
    """Quick, non-blocking, non-disturbing lock inspection (for doctor)."""
    if not path.exists():
        return LockStatus("free")
    try:
        handle = _open_lock_file(path)
    except OSError:
        return LockStatus("unknown")
    try:
        if _try_lock(handle):
            _unlock(handle)
            return LockStatus("free")
        return LockStatus("held", read_lock_owner(path))
    finally:
        handle.close()


class RunLock:
    """Cross-platform exclusive file lock with bounded acquisition.

    Uses non-blocking fcntl.flock on POSIX and msvcrt.locking on Windows,
    retried until a monotonic deadline. After winning the OS lock, writes
    diagnostic owner metadata (pid/operation/source/host/time). That
    metadata is informational only: the OS lock is authoritative, and the
    lock file is never deleted or force-unlocked based on it.
    """

    def __init__(
        self,
        path: Path,
        *,
        operation: str | None = None,
        source_id: str | None = None,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._path = path
        self._operation = operation or path.stem
        self._source_id = source_id
        self._timeout = timeout_seconds
        self._handle: Any = None

    def acquire(self, timeout_seconds: float | None = None) -> None:
        if self._handle is not None:
            raise RuntimeError(f"RunLock for {self._path.name} is already acquired")
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
        handle = _open_lock_file(self._path)
        try:
            log_event(
                _logger,
                "lock_wait_started",
                lock=self._path.name,
                operation=self._operation,
                source_id=self._source_id,
                timeout_seconds=timeout,
            )
            started = time.monotonic()
            deadline = started + timeout
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    owner = read_lock_owner(self._path)
                    log_event(
                        _logger,
                        "lock_wait_timeout",
                        level=logging.WARNING,
                        lock=self._path.name,
                        operation=self._operation,
                        source_id=self._source_id,
                        timeout_seconds=timeout,
                        owner_pid=(owner or {}).get("pid"),
                        owner_operation=(owner or {}).get("operation"),
                        owner_source_id=(owner or {}).get("source_id"),
                    )
                    raise RunLockTimeoutError(str(self._path), timeout, owner=owner)
                time.sleep(_LOCK_POLL_SECONDS)
            self._write_metadata(handle)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        log_event(
            _logger,
            "lock_acquired",
            lock=self._path.name,
            operation=self._operation,
            source_id=self._source_id,
            waited_seconds=round(time.monotonic() - started, 3),
        )

    def _write_metadata(self, handle: Any) -> None:
        meta = {
            "schema_version": _METADATA_SCHEMA_VERSION,
            "pid": os.getpid(),
            "operation": _sanitize_token(self._operation),
            "source_id": _sanitize_token(self._source_id),
            "hostname": _sanitize_token(socket.gethostname()),
            "acquired_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            handle.seek(_METADATA_OFFSET)
            handle.truncate()
            handle.write(json.dumps(meta).encode("utf-8"))
            handle.flush()
        except OSError:
            pass  # diagnostics only; never fail a lock we already hold

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            with contextlib.suppress(OSError):
                handle.seek(_METADATA_OFFSET)
                handle.truncate()
                handle.flush()
            _unlock(handle)
        finally:
            handle.close()
            log_event(
                _logger,
                "lock_released",
                lock=self._path.name,
                operation=self._operation,
                source_id=self._source_id,
            )


@dataclass
class AppContext:
    config: RagMonkConfig
    home: Path
    cwd: Path
    sources_conn: sqlite3.Connection
    _project_conns: dict[str, sqlite3.Connection] = field(default_factory=dict)
    _lock: RunLock | None = None
    # Storage backend abstraction plan, Phase 6: one server ``KnowledgeBackend``
    # per ``AppContext`` (process/session), lazily constructed and cached here
    # -- see ``backend()`` below. Local mode never populates this; it always
    # wraps the already-cached ``project_conn`` instead, so there is nothing
    # to cache at this level for local mode.
    _server_backend: KnowledgeBackend | None = None

    @classmethod
    def bootstrap(
        cls,
        *,
        home: Path | None = None,
        cwd: Path | None = None,
        cli_overrides: dict[str, Any] | None = None,
        log_console_format: str = "text",
    ) -> AppContext:
        resolved_home = paths.ensure_runtime_layout(home)
        resolved_cwd = cwd or Path.cwd()
        config = load_config(home=resolved_home, cwd=resolved_cwd, cli_overrides=cli_overrides)
        configure_logging(
            logs_dir=paths.logs_dir(resolved_home),
            level=config.runtime.log_level,
            console_format=log_console_format,
        )
        sources_conn = connect(
            paths.sources_db_path(resolved_home),
            cache_size_mb=config.runtime.sqlite_cache_size_mb,
        )
        apply_migrations(sources_conn, "sources")
        # CLI performance improvement plan, Phase 4: every "normal" command
        # goes through this one bootstrap path (unlike `version`/`--help`/
        # `config`/`update`, none of which call it -- see the plan's
        # "commands that must never trigger network update checking" list).
        # This only ever *decides* whether to spawn a detached background
        # checker from the local cache's age; it never itself makes a
        # network call, and a failure here must never break the command
        # that happens to trigger it.
        with contextlib.suppress(Exception):  # see this block's comment above
            update_background.maybe_launch_background_check(resolved_home, config.updates)
        return cls(config=config, home=resolved_home, cwd=resolved_cwd, sources_conn=sources_conn)

    def project_conn(self, project_id: str, *, control_plane: bool = False) -> sqlite3.Connection:
        """The per-project local sqlite (``knowledge.db``) connection.

        Independent review BLOCKER fix (storage.mode bypass): this is the
        single choke point every call site in the codebase goes through to
        reach local per-project sqlite, so the ``storage.mode`` guard lives
        here rather than being copy-pasted into each of the many call
        sites -- ``code.graph.all_project_connections``/
        ``conn_for_source_path`` already established this exact pattern
        (raise, never silently fall back to local sqlite in server mode);
        this closes the gap where ``project_conn`` itself had no such
        guard, so anything calling it directly bypassed those two
        already-guarded functions entirely.

        ``control_plane=True`` is a narrow, explicit opt-out for call
        sites that are genuinely NOT reading/writing searchable knowledge
        data, but the local, per-process bookkeeping that Phase 6/7/8's
        own design keeps local even when ``storage.mode == "server"``
        (schema/version bookkeeping, the indexing coordinator's own
        scan/generation state, the local job queue). Every call site
        passing it carries its own comment explaining why. Default
        ``False`` is deliberately the safer failure mode: an oversight at
        a new call site raises loudly in server mode instead of silently
        returning wrong/stale knowledge data, per this plan's "never
        silently fall back" rule.
        """
        if not control_plane and self.config.storage.mode != "local":
            raise LocalStorageModeRequiredError(
                "AppContext.project_conn(): storage.mode is "
                f"{self.config.storage.mode!r}, not 'local' -- local "
                "per-project sqlite must never be read/written for "
                "knowledge data outside local mode (no silent local "
                "fallback in server mode); callers must route through "
                "ctx.backend() instead"
            )
        conn = self._project_conns.get(project_id)
        if conn is None:
            paths.ensure_project_layout(project_id, self.home)
            conn = connect(
                paths.project_db_path(project_id, self.home),
                cache_size_mb=self.config.runtime.sqlite_cache_size_mb,
            )
            apply_migrations(conn, "knowledge")
            self._project_conns[project_id] = conn
        return conn

    def backend(self, project_id: str | None = None) -> KnowledgeBackend:
        """The single ``KnowledgeBackend`` CLI commands, MCP tools, and the
        Admin UI must all read/write knowledge through (Storage backend
        abstraction plan, Phase 6).

        - ``storage.mode == "server"``: returns the SAME cached instance
          (constructed once via ``backends.factory.create_backend``) on
          every call, regardless of ``project_id`` -- a server backend is a
          single source of truth across every source, not a per-project
          handle, so ``project_id`` is accepted-and-ignored here purely so
          call sites don't need an ``if server: ... else: ...`` branch just
          to pick this method's arguments.
        - ``storage.mode == "local"`` (default): wraps this context's
          already-cached ``project_conn(project_id)`` in a fresh
          ``LocalKnowledgeBackend(conn=...)`` -- the same "externally-owned
          connection" shape P3's ``indexing/runner.py`` already uses, so a
          call through this method lands in the exact same connection the
          rest of that project's reads/writes use. ``project_id`` is
          required in this mode: there is no single local connection that
          spans every project.
        """
        if self.config.storage.mode == "server":
            if self._server_backend is None:
                from ragmonk.backends.factory import create_backend

                self._server_backend = create_backend(self.config.storage, home=self.home)
            return self._server_backend

        if project_id is None:
            raise ValueError(
                "AppContext.backend(): project_id is required in local mode "
                "(storage.mode == 'local') -- there is no single local "
                "connection spanning every project"
            )
        from ragmonk.backends.local import LocalKnowledgeBackend

        return LocalKnowledgeBackend(conn=self.project_conn(project_id))

    def close_project_conn(self, project_id: str) -> None:
        """Drops and closes one cached project connection, if open.

        Used by ``ragmonk rebuild`` (``ops/rebuild.py``) before deleting a
        project's ``knowledge.db`` file out from under it -- an open WAL
        connection holding the old file would otherwise keep its inode
        alive/locked instead of the delete taking effect cleanly, and the
        next ``project_conn`` call recreates a fresh, freshly-migrated
        connection against the new (absent) file.
        """
        conn = self._project_conns.pop(project_id, None)
        if conn is not None:
            conn.close()

    def _new_lock(self, name: str, operation: str | None, source_id: str | None) -> RunLock:
        return RunLock(
            paths.locks_dir(self.home) / f"{name}.lock",
            operation=operation or name,
            source_id=source_id,
            timeout_seconds=self.config.indexing.lock_timeout_seconds,
        )

    def acquire_lock(
        self, name: str, *, operation: str | None = None, source_id: str | None = None
    ) -> RunLock:
        """Bounded acquisition (``indexing.lock_timeout_seconds``); raises
        ``RunLockTimeoutError`` rather than ever waiting forever.
        """
        lock = self._new_lock(name, operation, source_id)
        lock.acquire()
        self._lock = lock
        return lock

    @contextlib.contextmanager
    def index_lock(
        self, *, operation: str = "index", source_id: str | None = None
    ) -> Iterator[RunLock]:
        """Scoped ``index.lock`` hold, released even on error. Does not use
        the single ``_lock`` slot, so it can be taken repeatedly.
        """
        lock = self._new_lock("index", operation, source_id)
        lock.acquire()
        try:
            yield lock
        finally:
            lock.release()

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self._server_backend is not None:
            self._server_backend.close()
            self._server_backend = None
        self.sources_conn.close()
        for conn in self._project_conns.values():
            conn.close()
        self._project_conns.clear()
        # Printed last, after a command's own output (matches this plan's
        # own worked examples: results first, notice appended below) --
        # reads the local cache only, see notifier.py. Never allowed to
        # break cleanup or the command's own exit code.
        with contextlib.suppress(Exception):  # see this block's comment above
            update_notifier.maybe_notify(self.home, self.config.updates)

    def __enter__(self) -> AppContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
