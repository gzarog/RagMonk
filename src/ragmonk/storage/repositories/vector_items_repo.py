"""CRUD for ``vector_items`` (blueprint section 13): the integer-key <->
subject mapping a persistent ANN index needs, plus the batched metadata
lookup (blueprint section 47) that turns a handful of ANN search hits
into ``retrieval/semantic.py``'s ``SemanticHit`` rows in one query
instead of one ``entities_repo.get``/``documents_repo.get_unit`` round
trip per hit.

Regenerating a file's vector items is delete-then-insert under the same
caller-held transaction as its embeddings (see
``indexing/embedding_indexer.py``), mirroring every other derived table
in this project's generational write pattern.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


def delete_by_file(conn: sqlite3.Connection, file_id: str) -> None:
    conn.execute("DELETE FROM vector_items WHERE file_id = ?", (file_id,))


def list_vector_ids_by_file(conn: sqlite3.Connection, file_ids: Sequence[str]) -> list[int]:
    """The vector ids a set of files currently own, read *before* a
    reindex's ``delete_by_file`` call -- the caller needs this "before"
    snapshot to know which ids to remove from the ANN index itself
    (``retrieval/ann.py``), since the index has no way to discover that
    on its own once the owning rows are gone.
    """
    if not file_ids:
        return []
    placeholders = ", ".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT vector_id FROM vector_items WHERE file_id IN ({placeholders})",
        tuple(file_ids),
    ).fetchall()
    return [row["vector_id"] for row in rows]


def insert(
    conn: sqlite3.Connection,
    *,
    subject_type: str,
    subject_id: str,
    file_id: str,
    source_id: str,
    model_id: str,
) -> int:
    """Assigns a fresh, never-reused ``vector_id`` (``AUTOINCREMENT``) to
    one embedding subject and returns it -- the id ``retrieval/ann.py``
    adds to the ANN index under.
    """
    cursor = conn.execute(
        """
        INSERT INTO vector_items (subject_type, subject_id, file_id, source_id, model_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (subject_type, subject_id, file_id, source_id, model_id),
    )
    vector_id = cursor.lastrowid
    assert vector_id is not None  # AUTOINCREMENT PK always yields one
    return vector_id


def list_with_vectors_by_file(
    conn: sqlite3.Connection, file_ids: Sequence[str], *, model_id: str
) -> list[tuple[int, list[float]]]:
    """``(vector_id, vector)`` pairs for a set of files' *current*
    generation of vector items -- read after the embeddings/vector_items
    transaction has committed, so this is exactly what the ANN index
    should now contain for those files (blueprint section 14's
    incremental update flow).
    """
    if not file_ids:
        return []
    from ragmonk.storage.repositories.embeddings_repo import unpack_vector

    placeholders = ", ".join("?" for _ in file_ids)
    rows = conn.execute(
        f"""
        SELECT vi.vector_id, e.vector
        FROM vector_items vi
        JOIN embeddings e
            ON e.subject_type = vi.subject_type
           AND e.subject_id = vi.subject_id
           AND e.model_id = vi.model_id
        WHERE vi.file_id IN ({placeholders}) AND vi.model_id = ?
        """,
        (*file_ids, model_id),
    ).fetchall()
    return [(row["vector_id"], unpack_vector(row["vector"])) for row in rows]


def count_all(conn: sqlite3.Connection, *, model_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM vector_items WHERE model_id = ?", (model_id,)
    ).fetchone()
    return int(row["n"])


def list_all_with_vectors(
    conn: sqlite3.Connection, *, model_id: str
) -> list[tuple[int, list[float]]]:
    """Every current-model ``(vector_id, vector)`` pair -- used to
    rebuild the ANN index from scratch (``ragmonk vectors rebuild``,
    blueprint section 15), SQLite being the always-authoritative source
    an ANN index can be regenerated from.
    """
    from ragmonk.storage.repositories.embeddings_repo import unpack_vector

    rows = conn.execute(
        """
        SELECT vi.vector_id, e.vector
        FROM vector_items vi
        JOIN embeddings e
            ON e.subject_type = vi.subject_type
           AND e.subject_id = vi.subject_id
           AND e.model_id = vi.model_id
        WHERE vi.model_id = ?
        """,
        (model_id,),
    ).fetchall()
    return [(row["vector_id"], unpack_vector(row["vector"])) for row in rows]


@dataclass(slots=True, frozen=True)
class VectorMetadataRow:
    """One ANN hit's subject, already resolved to the shape ``retrieval/
    semantic.py`` needs to build a ``SemanticHit`` -- the batched
    replacement (blueprint section 47) for a per-hit
    ``entities_repo.get``/``documents_repo.get_unit`` round trip.
    """

    vector_id: int
    kind: str  # "entity" | "document"
    id: str
    title: str
    path: str
    snippet: str
    location: dict[str, object] | None


def batch_metadata_lookup(
    conn: sqlite3.Connection, vector_ids: Sequence[int]
) -> dict[int, VectorMetadataRow]:
    """Resolves every ``vector_id`` in one query each against ``entities``
    and ``document_sections`` (a vector subject is exactly one of the
    two, see ``core.models.EmbeddingSubjectType``), replacing what would
    otherwise be one metadata round trip per ANN search hit.
    """
    if not vector_ids:
        return {}
    placeholders = ", ".join("?" for _ in vector_ids)
    results: dict[int, VectorMetadataRow] = {}

    entity_rows = conn.execute(
        f"""
        SELECT vi.vector_id, e.id, e.qualified_name, e.signature,
               e.start_line, e.end_line, f.path
        FROM vector_items vi
        JOIN entities e ON e.id = vi.subject_id
        JOIN files f ON f.id = e.file_id
        WHERE vi.vector_id IN ({placeholders}) AND vi.subject_type = 'entity'
        """,
        tuple(vector_ids),
    ).fetchall()
    for row in entity_rows:
        results[row["vector_id"]] = VectorMetadataRow(
            vector_id=row["vector_id"],
            kind="entity",
            id=row["id"],
            title=row["qualified_name"],
            path=row["path"],
            snippet=row["signature"] or row["qualified_name"],
            location={"line_start": row["start_line"], "line_end": row["end_line"]},
        )

    document_rows = conn.execute(
        f"""
        SELECT vi.vector_id, ds.id, ds.text, ds.heading_path, ds.kind AS section_kind,
               ds.table_rows, d.title AS document_title, f.path
        FROM vector_items vi
        JOIN document_sections ds ON ds.id = vi.subject_id
        JOIN documents d ON d.id = ds.document_id
        JOIN files f ON f.id = ds.file_id
        WHERE vi.vector_id IN ({placeholders}) AND vi.subject_type = 'document_section'
        """,
        tuple(vector_ids),
    ).fetchall()
    for row in document_rows:
        text = row["text"] or ""
        if row["section_kind"] == "table" and row["table_rows"]:
            cells = json.loads(row["table_rows"])
            text = " ".join(cell for r in cells for cell in r if cell)
        heading_path = json.loads(row["heading_path"]) if row["heading_path"] else []
        title = row["document_title"] or row["path"]
        results[row["vector_id"]] = VectorMetadataRow(
            vector_id=row["vector_id"],
            kind="document",
            id=row["id"],
            title=title,
            path=row["path"],
            snippet=text[:280],
            location={"section": " > ".join(heading_path) if heading_path else None},
        )

    return results
