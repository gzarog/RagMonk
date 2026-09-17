"""Storage-level tests for ``cross_links``: stale-link cleanup on
generational regeneration/deletion (mirroring Phase 1-3's pattern), and
proof that an explicit user mapping survives an automated linking pass.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ragmonk.core.models import (
    Confidence,
    CrossLink,
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
    RelationshipType,
)
from ragmonk.knowledge.linker import link_touched_files
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo, entities_repo, files_repo, links_repo
from ragmonk.storage.sqlite import connect, transaction


def _seed_code_file(conn, *, file_id: str = "code_f1", path: str = "/repo/pkg/dog.py") -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id=file_id,
            source_id="s1",
            path=path,
            kind=FileKind.CODE,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def _seed_doc_file(conn, *, file_id: str = "doc_f1", path: str = "/repo/docs/manual.md") -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id=file_id,
            source_id="s1",
            path=path,
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def _entity(entity_id: str, *, file_id: str = "code_f1") -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name="bark",
        qualified_name="pkg.Dog.bark",
        language="python",
        start_line=1,
        end_line=2,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def _seed_document(conn, *, document_id: str = "doc1", file_id: str = "doc_f1") -> None:  # noqa: ANN001
    documents_repo.insert_document(
        conn,
        Document(
            id=document_id,
            source_id="s1",
            file_id=file_id,
            format=DocumentFormat.MARKDOWN,
            title="Manual",
            generation=1,
            created_at="now",
            updated_at="now",
        ),
    )


def _link(
    *, entity_id: str, document_id: str, resolver: str, section_id: str | None = None
) -> CrossLink:
    return CrossLink(
        id=uuid.uuid4().hex,
        link_type=RelationshipType.DOCUMENTED_BY,
        entity_id=entity_id,
        document_id=document_id,
        section_id=section_id,
        resolver=resolver,
        confidence=Confidence.EXACT if resolver == "user" else Confidence.HIGH,
        evidence=None,
        created_at="now",
    )


def test_stale_links_removed_when_code_file_regenerated(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_code_file(conn)
        _seed_doc_file(conn)
        with transaction(conn):
            entities_repo.insert(conn, _entity("e1"), snippet="def bark(self): ...")
            _seed_document(conn)
            links_repo.insert(
                conn,
                _link(entity_id="e1", document_id="doc1", resolver="linker:exact_identifier"),
            )

        assert links_repo.list_all(conn) != []

        # Simulate re-indexing code_f1 with changed content: the code
        # processor always deletes-then-reinserts a file's entities under
        # fresh ids (see code/processor.py), so any link pinned to the
        # old entity id must go with it or it would dangle against a
        # foreign key that no longer resolves.
        with transaction(conn):
            entities_repo.delete_by_file(conn, "code_f1")

        assert links_repo.list_all(conn) == []
    finally:
        conn.close()


def test_stale_links_removed_when_document_file_regenerated(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_code_file(conn)
        _seed_doc_file(conn)
        with transaction(conn):
            entities_repo.insert(conn, _entity("e1"), snippet="def bark(self): ...")
            _seed_document(conn)
            links_repo.insert(
                conn,
                _link(entity_id="e1", document_id="doc1", resolver="linker:exact_identifier"),
            )

        assert links_repo.list_all(conn) != []

        with transaction(conn):
            documents_repo.delete_by_file(conn, "doc_f1")

        assert links_repo.list_all(conn) == []
    finally:
        conn.close()


def test_stale_links_removed_when_file_deleted(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_code_file(conn)
        _seed_doc_file(conn)
        with transaction(conn):
            entities_repo.insert(conn, _entity("e1"), snippet="def bark(self): ...")
            _seed_document(conn)
            links_repo.insert(
                conn,
                _link(entity_id="e1", document_id="doc1", resolver="linker:exact_identifier"),
            )

        assert links_repo.list_all(conn) != []

        files_repo.delete(conn, "code_f1")

        assert links_repo.list_all(conn) == []
    finally:
        conn.close()


def test_explicit_user_link_survives_automated_linking_pass(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_code_file(conn)
        _seed_doc_file(conn)
        with transaction(conn):
            entities_repo.insert(conn, _entity("e1"), snippet="def bark(self): ...")
            _seed_document(conn)
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p1",
                    document_id="doc1",
                    file_id="doc_f1",
                    text="pkg.Dog.bark is the core API.",
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Manual",
            )

        user_link = _link(entity_id="e1", document_id="doc1", resolver="user")
        with transaction(conn):
            assert links_repo.insert(conn, user_link)

        with transaction(conn):
            inserted = link_touched_files(
                conn,
                touched_code_file_ids=["code_f1"],
                touched_document_file_ids=["doc_f1"],
            )

        # The automated pass found real, separate evidence (the qualified
        # name appears in the paragraph) and recorded it as its own row --
        # it never touched, rewrote, or deleted the user's row.
        assert inserted > 0
        all_links = links_repo.list_by_entity(conn, "e1")
        user_rows = [link for link in all_links if link.resolver == "user"]
        assert len(user_rows) == 1
        assert user_rows[0].id == user_link.id
        assert user_rows[0].confidence is Confidence.EXACT
        assert user_rows[0].section_id is None

        auto_rows = [link for link in all_links if link.resolver != "user"]
        assert auto_rows != []
        assert all(link.confidence is not Confidence.EXACT for link in auto_rows)
    finally:
        conn.close()
