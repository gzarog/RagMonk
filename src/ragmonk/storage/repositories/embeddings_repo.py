"""CRUD for ``embeddings`` in a project's ``knowledge.db`` (Phase 9).

Regenerating a file's embeddings is delete-then-insert, mirroring
``entities_repo``/``documents_repo``'s own generational pattern -- see
``indexing/embedding_indexer.py``, the only writer. Vectors are packed as
raw little-endian float32 bytes (``array.array('f', ...)``), not JSON: a
384-float JSON array is ~5x the bytes and needs re-parsing on every read,
for no benefit this table's one consumer (brute-force cosine similarity,
``retrieval/vectorstore.py``) actually needs.
"""

from __future__ import annotations

import array
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ragmonk.core.models import EmbeddingSubjectType

# The typecode literal is repeated inline (not hoisted into a shared
# constant) because mypy's ``array.array`` stub overloads resolve the
# element type from a literal typecode argument, not a ``str`` variable
# -- keeping it a literal here is what makes ``unpack_vector`` below
# type-check as ``list[float]`` rather than falling back to ``list[int]``.


@dataclass(frozen=True)
class EmbeddingRow:
    id: str
    subject_type: EmbeddingSubjectType
    subject_id: str
    file_id: str
    source_id: str
    model_id: str
    dim: int
    vector: list[float]
    generation: int


def pack_vector(vector: Sequence[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    arr: array.array[float] = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


def _row_to_embedding(row: sqlite3.Row) -> EmbeddingRow:
    return EmbeddingRow(
        id=row["id"],
        subject_type=EmbeddingSubjectType(row["subject_type"]),
        subject_id=row["subject_id"],
        file_id=row["file_id"],
        source_id=row["source_id"],
        model_id=row["model_id"],
        dim=row["dim"],
        vector=unpack_vector(row["vector"]),
        generation=row["generation"],
    )


def delete_by_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Removes every embedding derived from ``file_id`` (any model,
    any subject) -- always run inside the same caller-held transaction as
    the subsequent inserts, mirroring ``entities_repo.delete_by_file``.
    """
    conn.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))


def delete_by_files(conn: sqlite3.Connection, file_ids: Sequence[str]) -> None:
    """Same as ``delete_by_file``, for several files in one statement --
    indexing optimization plan V2, Phase P4 (measured):
    ``embedding_indexer.publish_embeddings`` deletes every touched file's
    previous-generation embeddings inside one already-held transaction
    covering the whole batch (unchanged from Phase P5), so folding N
    single-file ``DELETE``s into one ``IN (...)`` delete changes nothing
    about failure semantics -- see ``files_repo.
    update_embedding_version_many``'s identical rationale. A no-op for an
    empty ``file_ids``.
    """
    if not file_ids:
        return
    placeholders = ", ".join("?" for _ in file_ids)
    conn.execute(f"DELETE FROM embeddings WHERE file_id IN ({placeholders})", tuple(file_ids))


def insert(
    conn: sqlite3.Connection,
    *,
    subject_type: EmbeddingSubjectType,
    subject_id: str,
    file_id: str,
    source_id: str,
    model_id: str,
    vector: Sequence[float],
    generation: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO embeddings (
            id, subject_type, subject_id, file_id, source_id, model_id,
            dim, vector, generation, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            subject_type.value,
            subject_id,
            file_id,
            source_id,
            model_id,
            len(vector),
            pack_vector(vector),
            generation,
            datetime.now(UTC).isoformat(),
        ),
    )


def list_by_model(conn: sqlite3.Connection, model_id: str) -> list[EmbeddingRow]:
    """Every current-model embedding in this project -- the candidate set
    ``retrieval/semantic.py`` scores against. Rows tagged with a
    different ``model_id`` (a since-changed embedding model) are simply
    excluded here rather than ever being compared against a query vector
    from a different model's vector space.
    """
    rows = conn.execute("SELECT * FROM embeddings WHERE model_id = ?", (model_id,)).fetchall()
    return [_row_to_embedding(row) for row in rows]


def count_all(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
    return int(row["n"])


def get_dim_for_model(conn: sqlite3.Connection, model_id: str) -> int | None:
    """The vector dimensionality currently stored for ``model_id``, or
    ``None`` if this project has no such embeddings yet -- an ANN index
    (``retrieval/ann.py``) needs a fixed ``ndim`` at construction time,
    so this is read from the data itself rather than assumed to be
    ``retrieval/embedder.EMBEDDING_DIM``: tests (and any future
    configurable embedding model) may use a different one.
    """
    row = conn.execute(
        "SELECT dim FROM embeddings WHERE model_id = ? LIMIT 1", (model_id,)
    ).fetchone()
    return int(row["dim"]) if row is not None else None


def clear_all(conn: sqlite3.Connection) -> None:
    """Drops every embedding in this project -- used by tests to prove
    semantic search degrades gracefully when embeddings are missing.
    Never touches ``entities``/``documents``/``document_sections``.
    """
    conn.execute("DELETE FROM embeddings")
