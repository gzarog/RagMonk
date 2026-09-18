"""``ragmonk backup``: an online, consistent snapshot of every database
RagMonk maintains, packaged into one restorable archive.

Contains: ``sources.db``, every registered source's ``knowledge.db``, the
user config, and a manifest (RagMonk version, per-database schema
version, timestamp, included sources/projects). Deliberately does NOT
contain the original source files -- those are the user's own data
elsewhere on disk and are the blueprint's source of truth; only
derived/registry state is backed up here. ``.tar.gz`` was chosen as the
archive format: it is a single file (easy to move/store), widely
supported, and compresses SQLite files (mostly-empty pages, repetitive
schema text) well without any RagMonk-specific tooling required to
inspect it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ragmonk import __version__
from ragmonk.core import paths
from ragmonk.core.models import Source
from ragmonk.storage.migrations import current_version

# Versions the archive *layout itself* (manifest shape, member names) --
# independent of any individual database's schema version, so a future
# change to how a backup is packaged (not to the DBs it contains) can
# still be detected and refused by an older ``ragmonk restore``.
MANIFEST_FORMAT_VERSION = 1


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    ragmonk_version: str
    created_at: str
    sources_schema_version: int
    # project_id -> that project's knowledge.db schema version, for every
    # project actually included in the archive (a registered source with
    # no knowledge.db yet -- never indexed -- contributes nothing to back up).
    projects: dict[str, int] = field(default_factory=dict)
    sources: list[dict[str, str]] = field(default_factory=list)


def _read_schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    # ``current_version`` (and ``sources_repo``, used by callers of this
    # module) read columns by name (``row["v"]``) -- sqlite3's default row
    # factory returns plain tuples, so this must match the row factory
    # ``storage/sqlite.py``'s ``connect()`` sets for every other connection
    # in the codebase.
    conn.row_factory = sqlite3.Row
    try:
        return current_version(conn)
    finally:
        conn.close()


def _online_backup(src_path: Path, dest_path: Path) -> None:
    """Copies ``src_path`` to ``dest_path`` via SQLite's own backup API.

    Not a raw ``shutil.copy``: a WAL-mode database's on-disk state is
    split across the main file and a ``-wal`` file, and a plain file copy
    of just the main file can capture a torn/inconsistent snapshot (or
    silently miss committed-but-not-yet-checkpointed writes sitting only
    in the WAL). ``sqlite3.Connection.backup()`` instead reads through
    SQLite's own consistent-snapshot machinery, correct even while a
    writer holds the source database open, so this is also what makes a
    backup safe to run without stopping the daemon.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src_path))
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _default_backup_path(home: Path, created_at: str) -> Path:
    stamp = created_at.replace(":", "").replace("-", "").replace(".", "")
    return paths.backups_dir(home) / f"ragmonk-backup-{stamp}.tar.gz"


def create_backup(
    *, home: Path, sources: list[Source], dest: Path | None = None
) -> tuple[Path, BackupManifest]:
    """Builds one backup archive covering ``home``'s ``sources.db`` and
    every one of ``sources``' project databases that has actually been
    indexed at least once.

    Takes plain data (``home`` + a list of ``Source``) rather than an
    ``AppContext`` so both the ``ragmonk backup`` CLI command (which has
    a full context) and ``ragmonk upgrade`` (which must inspect versions
    *before* bootstrapping one, see ``ops/upgrade.py``) can call this the
    same way.
    """
    projects: dict[str, int] = {}
    source_entries: list[dict[str, str]] = []
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        db_path = paths.project_db_path(project_id, home)
        if db_path.is_file():
            projects[project_id] = _read_schema_version(db_path)
        source_entries.append({"id": source.id, "path": source.path, "project_id": project_id})

    sources_db_path = paths.sources_db_path(home)
    manifest = BackupManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        ragmonk_version=__version__,
        created_at=datetime.now(UTC).isoformat(),
        sources_schema_version=(
            _read_schema_version(sources_db_path) if sources_db_path.is_file() else 0
        ),
        projects=projects,
        sources=source_entries,
    )

    archive_path = dest if dest is not None else _default_backup_path(home, manifest.created_at)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    paths.tmp_dir(home).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.tmp_dir(home)) as tmp:
        tmp_dir = Path(tmp)

        if sources_db_path.is_file():
            _online_backup(sources_db_path, tmp_dir / "sources.db")

        for project_id in projects:
            _online_backup(
                paths.project_db_path(project_id, home),
                tmp_dir / "projects" / project_id / "knowledge.db",
            )

        config_src = paths.user_config_path(home)
        if config_src.is_file():
            shutil.copy2(config_src, tmp_dir / "config.yaml")

        (tmp_dir / "manifest.json").write_text(
            json.dumps(asdict(manifest), indent=2), encoding="utf-8"
        )

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(tmp_dir, arcname=".")

    return archive_path, manifest
