"""Direct unit test of ``code_fts``: proves it is populated and queryable
independently of the CLI, giving Phase 5's ``search`` command a working
substrate to build on.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import Entity, EntityType, FileKind, FileRecord, FileStatus
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import entities_repo, files_repo
from ragmonk.storage.sqlite import connect, transaction


def _seed_file(conn) -> None:  # noqa: ANN001 - test helper
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/tmp/pkg/dog.py",
            kind=FileKind.CODE,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def _entity(entity_id: str, name: str, qualified_name: str) -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=qualified_name,
        language="python",
        start_line=1,
        end_line=2,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def test_fts_returns_matching_entity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            entities_repo.insert(
                conn,
                _entity("e1", "bark", "pkg.Dog.bark"),
                snippet="def bark(self) -> str:",
            )
            entities_repo.insert(
                conn,
                _entity("e2", "speak", "pkg.Dog.speak"),
                snippet="def speak(self) -> str:",
            )

        results = entities_repo.search_fts(conn, "bark")
        assert [e.id for e in results] == ["e1"]
        assert results[0].qualified_name == "pkg.Dog.bark"
    finally:
        conn.close()


def test_fts_rows_removed_when_file_regenerated(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            entities_repo.insert(conn, _entity("e1", "bark", "pkg.Dog.bark"), snippet="bark")

        assert entities_repo.search_fts(conn, "bark") != []

        with transaction(conn):
            entities_repo.delete_by_file(conn, "f1")

        assert entities_repo.search_fts(conn, "bark") == []
    finally:
        conn.close()
