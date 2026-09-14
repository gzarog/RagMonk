"""``ragpilot restore``: reinstate a ``ragpilot backup`` archive as the
live runtime state.

Sequence (each step only proceeds once the previous one has succeeded):
verify the archive and its declared versions, stop a running daemon,
extract to a temporary staging location and run ``PRAGMA integrity_check``
on every database *there* (the live runtime directory is not touched at
all up to this point -- a failure at or before this step leaves it
completely untouched), then atomically swap the staged, verified state
into place, and finally restart the daemon if it had been running.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ragpilot.core import paths
from ragpilot.core.errors import DatabaseError, UsageError
from ragpilot.ops.backup import MANIFEST_FORMAT_VERSION
from ragpilot.service import pid
from ragpilot.storage.migrations import MIGRATIONS, DatabaseKind


@dataclass(frozen=True)
class RestoreReport:
    archive: str
    sources_restored: bool
    projects_restored: list[str] = field(default_factory=list)
    daemon_was_running: bool = False
    daemon_restarted: bool = False


def _latest_known_version(kind: DatabaseKind) -> int:
    return MIGRATIONS[kind][-1].version


def _extract_archive(archive: Path, staging: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # filter="data": rejects absolute paths, symlinks/hardlinks
            # escaping ``staging``, and device/special files -- an
            # archive is untrusted input the moment it could have come
            # from anywhere other than this machine's own ``ragpilot
            # backup``.
            tar.extractall(staging, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise DatabaseError(f"archive is corrupt or unreadable: {exc}") from exc


def _read_manifest(staging: Path) -> dict[str, Any]:
    manifest_path = staging / "manifest.json"
    if not manifest_path.is_file():
        raise DatabaseError("archive is missing manifest.json; refusing to restore")
    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatabaseError(f"archive manifest is unreadable: {exc}") from exc
    return data


def _check_integrity(db_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open {db_path.name} from archive: {exc}") from exc
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise DatabaseError(f"{db_path.name} is not a valid SQLite database: {exc}") from exc
    finally:
        conn.close()
    result = row[0] if row else "unknown"
    if result != "ok":
        raise DatabaseError(f"integrity check failed for {db_path.name}: {result}")


def _db_schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _move_aside(path: Path, holding: Path) -> Path | None:
    if not path.exists():
        return None
    target = holding / path.name
    os.rename(path, target)
    return target


def _put_back(target: Path, original: Path) -> None:
    if original.exists():
        if original.is_dir():
            shutil.rmtree(original)
        else:
            original.unlink()
    os.rename(target, original)


def _swap_into_place(home: Path, staging: Path, *, has_projects: bool, has_config: bool) -> None:
    """Renames the verified, staged state into place.

    Every live path this touches is first moved aside (not deleted) into
    ``holding`` under the same filesystem as the target (``paths.tmp_dir``
    lives under ``home``, same as ``sources.db``/``projects/``) so every
    individual step is a same-filesystem ``os.rename`` -- atomic, and
    reversible if a later step in this function fails: nothing is ever
    overwritten in place, so a crash mid-swap leaves either the untouched
    old state (still sitting in ``holding``, restored by the ``except``
    branch) or the fully-swapped new state, never a mix of the two.
    """
    holding = paths.tmp_dir(home) / f"pre_restore_{uuid.uuid4().hex[:12]}"
    holding.mkdir(parents=True, exist_ok=True)

    live_sources_db = paths.sources_db_path(home)
    live_projects = paths.projects_dir(home)
    live_config = paths.user_config_path(home)

    aside: list[tuple[Path, Path]] = []
    for suffix in ("", "-wal", "-shm"):
        live = live_sources_db.with_name(live_sources_db.name + suffix)
        moved = _move_aside(live, holding)
        if moved is not None:
            aside.append((moved, live))
    moved = _move_aside(live_projects, holding)
    if moved is not None:
        aside.append((moved, live_projects))
    if has_config:
        moved = _move_aside(live_config, holding)
        if moved is not None:
            aside.append((moved, live_config))

    try:
        os.rename(staging / "sources.db", live_sources_db)
        if has_projects:
            os.rename(staging / "projects", live_projects)
        else:
            live_projects.mkdir(parents=True, exist_ok=True)
        if has_config:
            os.rename(staging / "config.yaml", live_config)
    except Exception:
        for moved_target, original in reversed(aside):
            _put_back(moved_target, original)
        raise
    else:
        shutil.rmtree(holding, ignore_errors=True)


def restore_backup(archive: Path, *, home: Path, restart_daemon: bool = True) -> RestoreReport:
    if not archive.is_file():
        raise UsageError(f"no such archive: {archive}")

    paths.tmp_dir(home).mkdir(parents=True, exist_ok=True)
    staging = paths.tmp_dir(home) / f"restore_staging_{uuid.uuid4().hex[:12]}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        _extract_archive(archive, staging)
        manifest = _read_manifest(staging)

        format_version = manifest.get("format_version")
        if not isinstance(format_version, int) or format_version > MANIFEST_FORMAT_VERSION:
            raise DatabaseError(
                f"backup archive format v{format_version} is newer than this RAGpilot "
                f"understands (v{MANIFEST_FORMAT_VERSION}); refusing to restore"
            )

        staged_sources_db = staging / "sources.db"
        if not staged_sources_db.is_file():
            raise DatabaseError("archive is missing sources.db; refusing to restore")

        # Integrity first, then schema version: a corrupt/non-SQLite file
        # must be reported as "corrupt", not as a confusing schema-version
        # read failure -- ``_db_schema_version``'s own error handling only
        # covers the legitimate "no schema_migrations table yet" case, not
        # an outright invalid file.
        _check_integrity(staged_sources_db)
        sources_version = _db_schema_version(staged_sources_db)
        latest_sources = _latest_known_version("sources")
        if sources_version > latest_sources:
            raise DatabaseError(
                f"sources.db schema v{sources_version} is newer than this RAGpilot "
                f"version understands (v{latest_sources}); refusing to restore"
            )

        staged_projects_dir = staging / "projects"
        has_projects = staged_projects_dir.is_dir()
        project_ids: list[str] = []
        latest_knowledge = _latest_known_version("knowledge")
        if has_projects:
            for project_dir in sorted(staged_projects_dir.iterdir()):
                db_path = project_dir / "knowledge.db"
                if not db_path.is_file():
                    continue
                _check_integrity(db_path)
                version = _db_schema_version(db_path)
                if version > latest_knowledge:
                    raise DatabaseError(
                        f"project {project_dir.name} schema v{version} is newer than this "
                        f"RAGpilot version understands (v{latest_knowledge}); refusing to restore"
                    )
                project_ids.append(project_dir.name)

        has_config = (staging / "config.yaml").is_file()

        # Everything above only read the temp staging copy -- the live
        # runtime directory has not been touched, so any failure up to
        # here leaves it exactly as it was before this call.
        daemon_was_running = pid.stop_and_wait(
            home, action="restore into a live runtime directory", error_cls=DatabaseError
        )

        _swap_into_place(home, staging, has_projects=has_projects, has_config=has_config)

        daemon_restarted = False
        if daemon_was_running and restart_daemon:
            from ragpilot.cli import daemon as daemon_cli

            daemon_cli.start()
            daemon_restarted = True

        return RestoreReport(
            archive=str(archive),
            sources_restored=True,
            projects_restored=project_ids,
            daemon_was_running=daemon_was_running,
            daemon_restarted=daemon_restarted,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
