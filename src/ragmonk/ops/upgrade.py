"""``ragmonk upgrade``: explicit, backup-first orchestration of the
existing migration system across ``sources.db`` and every registered
project's ``knowledge.db``.

This does not reimplement migration application -- ``storage/
migrations.py``'s ``apply_migrations`` already does that, idempotently,
on every ``AppContext.bootstrap()``/``project_conn()`` call. What this
command adds: check what is pending *before* anything is touched (using
raw, read-only connections -- going through ``AppContext.bootstrap()``
first would apply ``sources.db``'s migrations as a side effect and defeat
the "before" half of a before/after report), take an automatic backup
(reusing ``ops/backup.py`` rather than a second implementation) when a
schema-changing migration is about to run, then let the normal bootstrap
path apply it, and finally reuse ``ragmonk doctor``'s own health check
to report whether the result looks healthy.

No automatic rollback: a full, safe auto-rollback of an already-applied
SQLite schema migration (reversing arbitrary DDL, including additive
``ALTER TABLE ADD COLUMN``, which SQLite cannot drop again before 3.35's
limited ``DROP COLUMN``) is a meaningfully separate and riskier feature
than this phase's priority (backup/restore/rebuild) warrants. The backup
taken above is the documented recovery path if ``healthy_after`` comes
back false: ``ragmonk restore <backup_archive>``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ragmonk.cli.doctor import overall_status, run_checks
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Source
from ragmonk.ops.backup import create_backup
from ragmonk.storage.migrations import MIGRATIONS, DatabaseKind, current_version
from ragmonk.storage.repositories import sources_repo


def _latest_version(kind: DatabaseKind) -> int:
    return MIGRATIONS[kind][-1].version


def _raw_connect(db_path: Path) -> sqlite3.Connection:
    """A read-only-in-effect connection (no writes issued through it) that
    still sets ``storage/sqlite.py``'s row factory, since
    ``current_version``/``sources_repo`` both read columns by name.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class DbVersionReport:
    name: str
    before: int
    after: int


@dataclass(frozen=True)
class UpgradeReport:
    pending: bool
    backup_archive: str | None
    databases: list[DbVersionReport] = field(default_factory=list)
    healthy_after: bool = True


def _read_registered_sources(home: Path) -> list[Source]:
    sources_db = paths.sources_db_path(home)
    if not sources_db.is_file():
        return []
    conn = _raw_connect(sources_db)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "sources" not in tables:
            return []
        return sources_repo.list_all(conn)
    finally:
        conn.close()


def check_pending(home: Path) -> tuple[int, dict[str, int]]:
    """Current schema version of ``sources.db`` and of every registered
    project's ``knowledge.db`` that has actually been created -- read-only,
    via raw connections, so calling this never itself applies a migration.
    """
    sources_db = paths.sources_db_path(home)
    sources_before = 0
    if sources_db.is_file():
        conn = _raw_connect(sources_db)
        try:
            sources_before = current_version(conn)
        finally:
            conn.close()

    projects_before: dict[str, int] = {}
    for source in _read_registered_sources(home):
        project_id = paths.project_id_for_path(Path(source.path))
        db_path = paths.project_db_path(project_id, home)
        if not db_path.is_file():
            continue
        conn = _raw_connect(db_path)
        try:
            projects_before[project_id] = current_version(conn)
        finally:
            conn.close()
    return sources_before, projects_before


def run_upgrade(*, home: Path) -> UpgradeReport:
    sources_before, projects_before = check_pending(home)
    pending = sources_before < _latest_version("sources") or any(
        v < _latest_version("knowledge") for v in projects_before.values()
    )

    backup_archive: str | None = None
    if pending:
        archive_path, _manifest = create_backup(home=home, sources=_read_registered_sources(home))
        backup_archive = str(archive_path)

    with AppContext.bootstrap(home=home) as ctx:
        sources_after = current_version(ctx.sources_conn)
        # control_plane=True: schema/version bookkeeping for each local
        # project db file, not a knowledge-data read -- these local
        # ``knowledge.db`` files (and their migrations) exist regardless
        # of ``storage.mode`` (Phase 7), so ``ragmonk upgrade`` must be
        # able to migrate them even when the configured mode is "server".
        projects_after = {
            project_id: current_version(ctx.project_conn(project_id, control_plane=True))
            for project_id in projects_before
        }
        healthy_after = overall_status(run_checks(ctx)) != "UNHEALTHY"

    databases = [DbVersionReport(name="sources", before=sources_before, after=sources_after)]
    for project_id in sorted(projects_before):
        databases.append(
            DbVersionReport(
                name=f"project:{project_id}",
                before=projects_before[project_id],
                after=projects_after[project_id],
            )
        )

    return UpgradeReport(
        pending=pending,
        backup_archive=backup_archive,
        databases=databases,
        healthy_after=healthy_after,
    )
