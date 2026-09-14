"""Source registration: validates a path, assigns a stable id, and keeps
the global ``sources`` table plus that source's project directory in sync.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragpilot.core import paths
from ragpilot.core.errors import RagpilotError, SourceUnavailableError, UsageError
from ragpilot.core.models import IndexingMode, Source, SourceType
from ragpilot.retrieval import cache as search_cache
from ragpilot.service import pid
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import sources_repo
from ragpilot.storage.sqlite import connect

_NETWORK_PREFIXES = ("\\\\", "//", "smb://", "nfs://", "afp://")


@dataclass(frozen=True)
class SourceRemoval:
    """What ``SourceRegistry.remove`` actually did, for the CLI to report --
    ``project_dir_deleted`` distinguishes "nothing to delete" from "deleted"
    since both are a successful, non-error outcome.
    """

    source: Source
    project_dir: Path
    project_dir_deleted: bool


def detect_source_type(raw_path: str) -> SourceType:
    if raw_path.startswith(_NETWORK_PREFIXES) and not raw_path.startswith("///"):
        return SourceType.NETWORK
    return SourceType.LOCAL


def make_source_id(canonical_path: str) -> str:
    return "src_" + hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:10]


class SourceRegistry:
    def __init__(self, conn: sqlite3.Connection, *, home: Path | None = None) -> None:
        self._conn = conn
        self._home = home

    def add(
        self,
        raw_path: str,
        *,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> Source:
        source_type = detect_source_type(raw_path)
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise SourceUnavailableError(f"source path does not exist: {raw_path}")
        canonical = str(path.resolve())
        if not path.is_dir():
            raise SourceUnavailableError(f"source path is not a directory: {canonical}")

        existing = sources_repo.get_by_path(self._conn, canonical)
        if existing is not None:
            return existing

        now = datetime.now(UTC).isoformat()
        source = Source(
            id=make_source_id(canonical),
            path=canonical,
            source_type=source_type,
            enabled=True,
            indexing_mode=IndexingMode.FULL,
            include_patterns=include_patterns or [],
            exclude_patterns=exclude_patterns or [],
            created_at=now,
            updated_at=now,
        )
        sources_repo.create(self._conn, source)

        project_id = paths.project_id_for_path(path)
        paths.ensure_project_layout(project_id, self._home)
        project_conn = connect(paths.project_db_path(project_id, self._home))
        try:
            apply_migrations(project_conn, "knowledge")
        finally:
            project_conn.close()
        return source

    def get(self, source_id: str) -> Source:
        source = sources_repo.get(self._conn, source_id)
        if source is None:
            raise UsageError(f"no such source: {source_id}")
        return source

    def list(self, *, enabled_only: bool = False) -> list[Source]:
        return sources_repo.list_all(self._conn, enabled_only=enabled_only)

    def set_enabled(self, source_id: str, enabled: bool) -> Source:
        self.get(source_id)
        sources_repo.set_enabled(
            self._conn, source_id, enabled, updated_at=datetime.now(UTC).isoformat()
        )
        return self.get(source_id)

    def _resolved_home(self) -> Path:
        return self._home or paths.runtime_dir()

    def project_dir_for(self, source: Source) -> Path:
        project_id = paths.project_id_for_path(Path(source.path))
        return paths.project_dir(project_id, self._home)

    def assert_no_active_daemon(self) -> None:
        """Guards every destructive operation this class performs: a
        running daemon owns its own watcher threads and reconciliation
        loop against ``self._home``, none of which this process can
        preempt or safely race a directory deletion against (see
        ``remove``'s docstring). Reused as-is rather than the blocking
        ``core.lifecycle.RunLock`` a daemon pass also takes, because that
        lock only serializes *writes* -- waiting on it here could still
        hang this interactive command indefinitely behind a daemon that
        keeps re-triggering itself, where failing fast with a fix
        ("stop the daemon first") is the safer, more honest answer.
        """
        if pid.running_daemon(self._resolved_home()) is not None:
            raise UsageError(
                "a RAGpilot daemon is running and may be actively indexing sources; "
                "run `ragpilot daemon stop` first, then retry."
            )

    def remove(self, source_id: str) -> SourceRemoval:
        """Deletes a source's entire derived project directory, then --
        only once that succeeds -- its ``sources`` row.

        This order is the whole point: if the filesystem delete fails
        partway through (permissions, a file locked by another process),
        the source stays registered rather than ending up in a
        half-removed state where it is neither indexed nor recoverable
        by a plain re-add. Deleting the directory wholesale (not
        enumerating its known files) is deliberate too -- any derived
        artifact a later phase adds under it is covered automatically,
        with nothing new to remember to delete here. The original source
        files on disk are never touched.
        """
        source = self.get(source_id)
        self.assert_no_active_daemon()
        project_dir = self.project_dir_for(source)

        deleted = project_dir.exists()
        if deleted:
            try:
                shutil.rmtree(project_dir)
            except OSError as exc:
                raise RagpilotError(
                    f"failed to delete project data at {project_dir}: {exc}; "
                    f"source '{source_id}' was not removed"
                ) from exc

        sources_repo.delete(self._conn, source_id)
        # See retrieval/cache.py's module docstring: both caches are
        # process-local and key themselves off each project database's
        # own on-disk identity, but a full reset is the simplest way to
        # guarantee a long-lived caller (``ragpilot serve``) never serves
        # a stale hit from a project whose database file just vanished.
        search_cache.reset_caches()
        return SourceRemoval(source=source, project_dir=project_dir, project_dir_deleted=deleted)
