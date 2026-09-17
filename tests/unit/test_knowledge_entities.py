"""Unit tests for ``knowledge/entities.py``'s lookup facade."""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import (
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
)
from ragmonk.knowledge import entities as knowledge_entities
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo, entities_repo, files_repo
from ragmonk.storage.sqlite import connect, transaction


def _seed(conn) -> None:  # noqa: ANN001 - test helper
    files_repo.insert(
        conn,
        FileRecord(
            id="code_f1",
            source_id="s1",
            path="/repo/pkg/dog.py",
            kind=FileKind.CODE,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    files_repo.insert(
        conn,
        FileRecord(
            id="doc_f1",
            source_id="s1",
            path="/repo/docs/manual.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    with transaction(conn):
        entities_repo.insert(
            conn,
            Entity(
                id="e1",
                source_id="s1",
                file_id="code_f1",
                kind=EntityType.FUNCTION,
                name="bark",
                qualified_name="pkg.Dog.bark",
                language="python",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark(self): ...",
        )
        documents_repo.insert_document(
            conn,
            Document(
                id="doc1",
                source_id="s1",
                file_id="doc_f1",
                format=DocumentFormat.MARKDOWN,
                title="Manual",
                generation=1,
                created_at="now",
                updated_at="now",
            ),
        )


def test_resolve_code_entity_by_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        assert knowledge_entities.resolve_code_entity(conn, "e1") is not None
    finally:
        conn.close()


def test_resolve_code_entity_by_qualified_name(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        entity = knowledge_entities.resolve_code_entity(conn, "pkg.Dog.bark")
        assert entity is not None
        assert entity.id == "e1"
    finally:
        conn.close()


def test_resolve_code_entity_unknown_is_none(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        assert knowledge_entities.resolve_code_entity(conn, "does.not.exist") is None
    finally:
        conn.close()


def test_resolve_document_by_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        assert knowledge_entities.resolve_document(conn, "doc1") is not None
    finally:
        conn.close()


def test_resolve_document_by_filename(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        document = knowledge_entities.resolve_document(conn, "manual.md")
        assert document is not None
        assert document.id == "doc1"
    finally:
        conn.close()


def test_resolve_document_by_full_path(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        document = knowledge_entities.resolve_document(conn, "/repo/docs/manual.md")
        assert document is not None
        assert document.id == "doc1"
    finally:
        conn.close()


def test_resolve_document_unknown_is_none(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        assert knowledge_entities.resolve_document(conn, "nope.pdf") is None
    finally:
        conn.close()
