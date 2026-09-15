from __future__ import annotations

import json
import tarfile
from pathlib import Path

from ragpilot import __version__
from ragpilot.core import paths
from ragpilot.core.models import IndexingMode, Source, SourceType
from ragpilot.ops.backup import MANIFEST_FORMAT_VERSION, create_backup
from ragpilot.storage.migrations import MIGRATIONS, apply_migrations
from ragpilot.storage.sqlite import connect

# The highest migration *version number*, not the count of migrations --
# ``current_version`` (what ``create_backup`` actually records per
# project) is ``MAX(version)`` from ``schema_migrations``, and this
# project's migration versions are not guaranteed contiguous (a version
# can be reserved by a not-yet-merged branch, leaving a gap here -- see
# ``storage/migrations/__init__.py``'s own numbering notes).
_KNOWLEDGE_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS["knowledge"])


def _make_source(path: str, *, now: str = "2026-01-01T00:00:00+00:00") -> Source:
    return Source(
        id="src_test",
        path=path,
        source_type=SourceType.LOCAL,
        indexing_mode=IndexingMode.FULL,
        created_at=now,
        updated_at=now,
    )


def test_create_backup_with_no_sources_still_backs_up_sources_db(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_runtime_layout(home)
    conn = connect(paths.sources_db_path(home))
    apply_migrations(conn, "sources")
    conn.close()

    archive_path, manifest = create_backup(home=home, sources=[])

    assert archive_path.is_file()
    assert manifest.format_version == MANIFEST_FORMAT_VERSION
    assert manifest.ragpilot_version == __version__
    assert manifest.sources_schema_version == 2
    assert manifest.projects == {}

    with tarfile.open(archive_path, "r:gz") as tar:
        names = set(tar.getnames())
    assert "./sources.db" in names
    assert "./manifest.json" in names
    assert not any(n.startswith("./projects") for n in names)


def test_create_backup_includes_every_indexed_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_runtime_layout(home)

    sources_conn = connect(paths.sources_db_path(home))
    apply_migrations(sources_conn, "sources")
    sources_conn.close()

    source_dir = tmp_path / "proj"
    source_dir.mkdir()
    source = _make_source(str(source_dir))
    project_id = paths.project_id_for_path(source_dir)
    paths.ensure_project_layout(project_id, home)
    project_conn = connect(paths.project_db_path(project_id, home))
    apply_migrations(project_conn, "knowledge")
    project_conn.close()

    archive_path, manifest = create_backup(home=home, sources=[source])

    assert manifest.projects == {project_id: _KNOWLEDGE_SCHEMA_VERSION}
    assert manifest.sources[0]["id"] == "src_test"
    assert manifest.sources[0]["project_id"] == project_id

    with tarfile.open(archive_path, "r:gz") as tar:
        member = tar.extractfile(f"./projects/{project_id}/knowledge.db")
        assert member is not None
        assert len(member.read()) > 0
        manifest_member = tar.extractfile("./manifest.json")
        assert manifest_member is not None
        on_disk_manifest = json.loads(manifest_member.read())
    assert on_disk_manifest["projects"] == {project_id: _KNOWLEDGE_SCHEMA_VERSION}


def test_create_backup_default_destination_lives_under_backups_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_runtime_layout(home)
    conn = connect(paths.sources_db_path(home))
    apply_migrations(conn, "sources")
    conn.close()

    archive_path, _manifest = create_backup(home=home, sources=[])

    assert archive_path.parent == paths.backups_dir(home)
    assert archive_path.suffixes[-2:] == [".tar", ".gz"]


def test_create_backup_explicit_destination_is_honored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_runtime_layout(home)
    conn = connect(paths.sources_db_path(home))
    apply_migrations(conn, "sources")
    conn.close()

    dest = tmp_path / "elsewhere" / "mine.tar.gz"
    archive_path, _manifest = create_backup(home=home, sources=[], dest=dest)

    assert archive_path == dest
    assert dest.is_file()
