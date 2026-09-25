"""Application context: config, DB connections, and a run lock, with a
graceful shutdown hook so every command starts and ends in the same way.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig, load_config
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect
from ragmonk.telemetry.logging import configure_logging
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


class RunLock:
    """Cross-platform exclusive file lock.

    Uses fcntl.flock on POSIX and msvcrt.locking on Windows.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w")  # noqa: SIM115 - handle outlives this call
        if fcntl is not None:
            fcntl.flock(self._handle, fcntl.LOCK_EX)
        elif msvcrt is not None:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self._handle.close()
        self._handle = None


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

    def project_conn(self, project_id: str) -> sqlite3.Connection:
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

    def acquire_lock(self, name: str) -> RunLock:
        lock = RunLock(paths.locks_dir(self.home) / f"{name}.lock")
        lock.acquire()
        self._lock = lock
        return lock

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
