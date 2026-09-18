"""Storage-level tests for ``embeddings``: vector pack/unpack round-trip,
delete-by-file scoping (mirroring ``entities_repo``/``documents_repo``'s
own generational pattern), and per-model filtering -- all with
precomputed fake vectors, no real embedding model involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.models import EmbeddingSubjectType
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import embeddings_repo
from ragmonk.storage.sqlite import connect, transaction


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def test_pack_unpack_round_trips_floats() -> None:
    vector = [0.1, -0.2, 0.375, 1.0, -1.0]
    packed = embeddings_repo.pack_vector(vector)
    unpacked = embeddings_repo.unpack_vector(packed)
    assert unpacked == pytest.approx(vector, abs=1e-6)


def test_insert_and_list_by_model(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e1",
            file_id="f1",
            source_id="s1",
            model_id="model-a",
            vector=[1.0, 2.0, 3.0],
        )
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.DOCUMENT_SECTION,
            subject_id="d1",
            file_id="f2",
            source_id="s1",
            model_id="model-b",
            vector=[4.0, 5.0],
        )

    rows_a = embeddings_repo.list_by_model(conn, "model-a")
    assert len(rows_a) == 1
    assert rows_a[0].subject_id == "e1"
    assert rows_a[0].subject_type is EmbeddingSubjectType.ENTITY
    assert rows_a[0].dim == 3
    assert rows_a[0].vector == pytest.approx([1.0, 2.0, 3.0])

    rows_b = embeddings_repo.list_by_model(conn, "model-b")
    assert len(rows_b) == 1
    assert rows_b[0].subject_id == "d1"

    assert embeddings_repo.count_all(conn) == 2


def test_delete_by_file_removes_only_that_files_rows(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e1",
            file_id="f1",
            source_id="s1",
            model_id="model-a",
            vector=[1.0, 0.0],
        )
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e2",
            file_id="f2",
            source_id="s1",
            model_id="model-a",
            vector=[0.0, 1.0],
        )

    with transaction(conn):
        embeddings_repo.delete_by_file(conn, "f1")

    remaining = embeddings_repo.list_by_model(conn, "model-a")
    assert [row.subject_id for row in remaining] == ["e2"]


def test_delete_by_file_removes_every_model_generation_for_that_file(conn) -> None:  # noqa: ANN001
    """A re-embed after the configured model changed must not leave an
    orphaned old-model row behind for the same file/subject -- see
    ``indexing/embedding_indexer.py``'s docstring.
    """
    with transaction(conn):
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e1",
            file_id="f1",
            source_id="s1",
            model_id="old-model",
            vector=[1.0, 0.0],
        )

    with transaction(conn):
        embeddings_repo.delete_by_file(conn, "f1")
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e1",
            file_id="f1",
            source_id="s1",
            model_id="new-model",
            vector=[0.0, 1.0],
        )

    assert embeddings_repo.list_by_model(conn, "old-model") == []
    new_rows = embeddings_repo.list_by_model(conn, "new-model")
    assert len(new_rows) == 1


def test_clear_all_empties_the_table(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e1",
            file_id="f1",
            source_id="s1",
            model_id="model-a",
            vector=[1.0],
        )
    assert embeddings_repo.count_all(conn) == 1
    embeddings_repo.clear_all(conn)
    assert embeddings_repo.count_all(conn) == 0
