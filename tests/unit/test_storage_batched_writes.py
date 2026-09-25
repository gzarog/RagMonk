"""Indexing optimization plan V2, Phase P4 (measured SQLite/write-path
completion): parity tests for the batched repository functions added
this phase -- each must return/affect exactly what looping the
single-row equivalent over the same ids would have, just in fewer
statements. See this phase's commit message for the statement-count/
wall-time measurements that justified keeping (or, for INSERT batching,
explicitly rejecting) each change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.models import (
    Document,
    DocumentFormat,
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


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def _insert_file(conn, file_id: str, kind: FileKind) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id=file_id,
            source_id="s1",
            path=f"/p/{file_id}",
            kind=kind,
            size=1,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def test_entities_list_by_files_matches_looping_list_by_file(conn) -> None:  # noqa: ANN001
    for fid in ("f1", "f2", "f3"):
        _insert_file(conn, fid, FileKind.CODE)
    with transaction(conn):
        for fid in ("f1", "f2", "f3"):
            for i in range(2):
                entities_repo.insert(
                    conn,
                    Entity(
                        id=f"{fid}-e{i}",
                        source_id="s1",
                        file_id=fid,
                        kind=EntityType.FUNCTION,
                        name=f"fn{i}",
                        qualified_name=f"{fid}.fn{i}",
                        language="python",
                        signature=f"def fn{i}():",
                        start_line=i,
                        end_line=i + 1,
                        generation=1,
                        created_at="now",
                        updated_at="now",
                    ),
                    snippet=f"def fn{i}(): ...",
                )

    looped = sorted(
        e.id for fid in ("f1", "f2", "f3") for e in entities_repo.list_by_file(conn, fid)
    )
    batched = sorted(e.id for e in entities_repo.list_by_files(conn, ["f1", "f2", "f3"]))
    assert looped == batched == ["f1-e0", "f1-e1", "f2-e0", "f2-e1", "f3-e0", "f3-e1"]

    # A subset (not every touched file) still returns exactly that
    # subset's rows -- no cross-file leakage.
    subset = sorted(e.id for e in entities_repo.list_by_files(conn, ["f2"]))
    assert subset == ["f2-e0", "f2-e1"]

    # Empty input -> empty output, no query issued.
    assert entities_repo.list_by_files(conn, []) == []


def test_documents_list_units_by_files_matches_looping_list_units_by_file(
    conn,
) -> None:  # noqa: ANN001
    for fid in ("f1", "f2"):
        _insert_file(conn, fid, FileKind.DOCUMENT)
    with transaction(conn):
        for fid in ("f1", "f2"):
            documents_repo.insert_document(
                conn,
                Document(
                    id=f"d-{fid}",
                    source_id="s1",
                    file_id=fid,
                    format=DocumentFormat.MARKDOWN,
                    title="Doc",
                    paragraph_count=1,
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id=f"p-{fid}",
                    document_id=f"d-{fid}",
                    file_id=fid,
                    text=f"text for {fid}",
                    heading_path=[],
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Doc",
                embedding_text=f"text for {fid}",
            )

    looped = sorted(
        u.id for fid in ("f1", "f2") for u in documents_repo.list_units_by_file(conn, fid)
    )
    batched = sorted(u.id for u in documents_repo.list_units_by_files(conn, ["f1", "f2"]))
    assert looped == batched == ["p-f1", "p-f2"]
    assert documents_repo.list_units_by_files(conn, []) == []


def test_embeddings_and_vector_items_delete_by_files_matches_per_file_deletes(
    conn,
) -> None:  # noqa: ANN001
    from ragmonk.core.models import EmbeddingSubjectType

    for fid in ("f1", "f2", "f3"):
        _insert_file(conn, fid, FileKind.DOCUMENT)
        with transaction(conn):
            embeddings_repo.insert(
                conn,
                subject_type=EmbeddingSubjectType.DOCUMENT_SECTION,
                subject_id=f"sub-{fid}",
                file_id=fid,
                source_id="s1",
                model_id="m1",
                vector=[0.1, 0.2],
            )
            vector_items_repo.insert(
                conn,
                subject_type="document_section",
                subject_id=f"sub-{fid}",
                file_id=fid,
                source_id="s1",
                model_id="m1",
            )

    assert embeddings_repo.count_all(conn) == 3

    with transaction(conn):
        embeddings_repo.delete_by_files(conn, ["f1", "f3"])
        vector_items_repo.delete_by_files(conn, ["f1", "f3"])

    remaining = embeddings_repo.list_by_model(conn, "m1")
    assert [e.file_id for e in remaining] == ["f2"]
    assert vector_items_repo.list_vector_ids_by_file(conn, ["f1", "f2", "f3"]) != []
    assert vector_items_repo.list_vector_ids_by_file(conn, ["f1", "f3"]) == []

    # Empty input is a no-op, not "delete everything".
    with transaction(conn):
        embeddings_repo.delete_by_files(conn, [])
    assert embeddings_repo.count_all(conn) == 1


def test_update_embedding_version_many_matches_per_file_updates(conn) -> None:  # noqa: ANN001
    for fid in ("f1", "f2", "f3"):
        _insert_file(conn, fid, FileKind.DOCUMENT)

    with transaction(conn):
        files_repo.update_embedding_version_many(
            conn,
            ["f1", "f3"],
            embedding_model_id="m2",
            embedding_text_version="v2",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    f1 = files_repo.get(conn, "f1")
    f2 = files_repo.get(conn, "f2")
    f3 = files_repo.get(conn, "f3")
    assert f1 is not None and f1.embedding_model_id == "m2"
    assert f3 is not None and f3.embedding_model_id == "m2"
    assert f2 is not None and f2.embedding_model_id is None  # untouched

    # Empty input touches nothing.
    with transaction(conn):
        files_repo.update_embedding_version_many(
            conn, [], embedding_model_id="m3", embedding_text_version="v3", updated_at="now"
        )
    f1_again = files_repo.get(conn, "f1")
    assert f1_again is not None and f1_again.embedding_model_id == "m2"  # unchanged
