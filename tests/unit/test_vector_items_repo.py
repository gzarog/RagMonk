"""Unit tests for ``storage/repositories/vector_items_repo.py``: the
integer vector-id <-> subject mapping table an ANN index needs
(blueprint section 13), and its batched metadata lookup (section 47).
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import (
    Document,
    DocumentFormat,
    EmbeddingSubjectType,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragmonk.storage.sqlite import connect, transaction

MODEL = "test-model"


def _file(file_id: str, path: str) -> FileRecord:
    return FileRecord(
        id=file_id,
        source_id="s1",
        path=path,
        kind=FileKind.CODE,
        size=1,
        mtime=0.0,
        status=FileStatus.QUEUED,
        created_at="now",
        updated_at="now",
    )


def _seed_entity_embedding(conn, *, entity_id: str, file_id: str, vector: list[float]) -> None:
    with transaction(conn):
        entities_repo.insert(
            conn,
            Entity(
                id=entity_id,
                source_id="s1",
                file_id=file_id,
                kind=EntityType.FUNCTION,
                name=entity_id,
                qualified_name=f"pkg.{entity_id}",
                language="python",
                signature=f"def {entity_id}(): ...",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet=f"def {entity_id}(): ...",
        )
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id=entity_id,
            file_id=file_id,
            source_id="s1",
            model_id=MODEL,
            vector=vector,
        )
        vector_items_repo.insert(
            conn,
            subject_type="entity",
            subject_id=entity_id,
            file_id=file_id,
            source_id="s1",
            model_id=MODEL,
        )


def test_insert_assigns_increasing_never_reused_ids(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "pkg/a.py"))
        _seed_entity_embedding(conn, entity_id="e1", file_id="f1", vector=[1.0, 0.0])
        _seed_entity_embedding(conn, entity_id="e2", file_id="f1", vector=[0.0, 1.0])

        ids = vector_items_repo.list_vector_ids_by_file(conn, ["f1"])
        assert len(ids) == 2
        assert ids[1] > ids[0]

        vector_items_repo.delete_by_file(conn, "f1")
        # Re-inserting after a delete must never reuse a freed id.
        _seed_entity_embedding(conn, entity_id="e3", file_id="f1", vector=[1.0, 1.0])
        new_ids = vector_items_repo.list_vector_ids_by_file(conn, ["f1"])
        assert new_ids[0] > max(ids)
    finally:
        conn.close()


def test_list_with_vectors_by_file_joins_embeddings(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "pkg/a.py"))
        _seed_entity_embedding(conn, entity_id="e1", file_id="f1", vector=[1.0, 0.0])

        rows = vector_items_repo.list_with_vectors_by_file(conn, ["f1"], model_id=MODEL)
        assert len(rows) == 1
        vector_id, vector = rows[0]
        assert vector == [1.0, 0.0]

        assert vector_items_repo.count_all(conn, model_id=MODEL) == 1
        all_rows = vector_items_repo.list_all_with_vectors(conn, model_id=MODEL)
        assert all_rows == rows
    finally:
        conn.close()


def test_batch_metadata_lookup_resolves_entities_and_documents(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "pkg/a.py"))
        files_repo.insert(conn, _file("f2", "docs/guide.md"))
        _seed_entity_embedding(conn, entity_id="e1", file_id="f1", vector=[1.0, 0.0])

        with transaction(conn):
            documents_repo.insert_document(
                conn,
                Document(
                    id="d1",
                    source_id="s1",
                    file_id="f2",
                    format=DocumentFormat.MARKDOWN,
                    title="Guide",
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p1",
                    document_id="d1",
                    file_id="f2",
                    text="Some paragraph text.",
                    heading_path=["Guide", "Intro"],
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Guide",
            )
            embeddings_repo.insert(
                conn,
                subject_type=EmbeddingSubjectType.DOCUMENT_SECTION,
                subject_id="p1",
                file_id="f2",
                source_id="s1",
                model_id=MODEL,
                vector=[0.0, 1.0],
            )
            vector_items_repo.insert(
                conn,
                subject_type="document_section",
                subject_id="p1",
                file_id="f2",
                source_id="s1",
                model_id=MODEL,
            )

        vector_ids = vector_items_repo.list_vector_ids_by_file(conn, ["f1", "f2"])
        assert len(vector_ids) == 2
        metadata = vector_items_repo.batch_metadata_lookup(conn, vector_ids)
        assert len(metadata) == 2

        entity_rows = [row for row in metadata.values() if row.kind == "entity"]
        assert entity_rows[0].id == "e1"
        assert entity_rows[0].title == "pkg.e1"
        assert entity_rows[0].path == "pkg/a.py"

        doc_rows = [row for row in metadata.values() if row.kind == "document"]
        assert doc_rows[0].id == "p1"
        assert doc_rows[0].title == "Guide"
        assert doc_rows[0].path == "docs/guide.md"
        assert doc_rows[0].snippet == "Some paragraph text."
        assert doc_rows[0].location == {"section": "Guide > Intro"}
    finally:
        conn.close()
