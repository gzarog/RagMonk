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

from ragpilot.core import paths
from ragpilot.core.config import RagpilotConfig, load_config
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.sqlite import connect
from ragpilot.telemetry.logging import configure_logging
from ragpilot.update import background as update_background
from ragpilot.update import notifier as update_notifier

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]


class RunLock:
    """Best-effort exclusive lock; a no-op fallback where flock is unavailable."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w")  # noqa: SIM115 - handle outlives this call
        if fcntl is not None:
            fcntl.flock(self._handle, fcntl.LOCK_EX)

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@dataclass
class AppContext:
    config: RagpilotConfig
    home: Path
    cwd: Path
    sources_conn: sqlite3.Connection
    _project_conns: dict[str, sqlite3.Connection] = field(default_factory=dict)
    _lock: RunLock | None = None

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

    def close_project_conn(self, project_id: str) -> None:
        """Drops and closes one cached project connection, if open.

        Used by ``ragpilot rebuild`` (``ops/rebuild.py``) before deleting a
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
