"""CRUD for the ``files`` table in a project's ``knowledge.db``."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from ragmonk.core.models import FileKind, FileRecord, FileStatus
from ragmonk.storage.repositories import links_repo
from ragmonk.storage.sqlite import transaction


def _row_to_file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        source_id=row["source_id"],
        path=row["path"],
        kind=FileKind(row["kind"]),
        size=row["size"],
        mtime=row["mtime"],
        content_hash=row["content_hash"],
        status=FileStatus(row["status"]),
        generation=row["generation"],
        parser_version=row["parser_version"],
        chunker_version=row["chunker_version"],
        embedding_model_id=row["embedding_model_id"],
        embedding_text_version=row["embedding_text_version"],
        last_indexed_at=row["last_indexed_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_by_path(conn: sqlite3.Connection, source_id: str, path: str) -> FileRecord | None:
    row = conn.execute(
        "SELECT * FROM files WHERE source_id = ? AND path = ?", (source_id, path)
    ).fetchone()
    return _row_to_file(row) if row is not None else None


def get(conn: sqlite3.Connection, file_id: str) -> FileRecord | None:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return _row_to_file(row) if row is not None else None


def get_many(conn: sqlite3.Connection, file_ids: Sequence[str]) -> dict[str, FileRecord]:
    """``{file_id: FileRecord}`` for every id in ``file_ids`` found, in one
    query -- indexing optimization plan Phase P6: a caller that needs
    several files' records (e.g. ``knowledge/linker.py``'s cross-domain
    linker, resolving each project file's filename for the "document
    mentions this filename" match) must never do it as N individual
    ``get()`` round trips, one per file, when a single ``IN (...)``
    query returns exactly the same rows. Missing ids are simply absent
    from the result, mirroring ``get()`` returning ``None`` for one.
    """
    if not file_ids:
        return {}
    placeholders = ", ".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT * FROM files WHERE id IN ({placeholders})", tuple(file_ids)
    ).fetchall()
    return {row["id"]: _row_to_file(row) for row in rows}


def list_by_source(conn: sqlite3.Connection, source_id: str) -> list[FileRecord]:
    rows = conn.execute(
        "SELECT * FROM files WHERE source_id = ? ORDER BY path", (source_id,)
    ).fetchall()
    return [_row_to_file(row) for row in rows]


def search_by_substring(
    conn: sqlite3.Connection, query: str, *, limit: int = 25
) -> list[FileRecord]:
    """Files whose path contains ``query``, path-ordered.

    A plain ``LIKE`` scan: the fallback ``search_path_projection`` below
    uses when ``path_fts`` finds nothing (e.g. a mid-word fragment like
    "ectorstore" that doesn't align to a token boundary) -- at the scale
    one local ``knowledge.db`` holds, that fallback scan is fast enough
    without needing its own index.
    """
    rows = conn.execute(
        "SELECT * FROM files WHERE path LIKE ? ORDER BY path LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    return [_row_to_file(row) for row in rows]


def _path_fts_query(text: str) -> str | None:
    """Every token ANDed together, unlike ``lexical.py``'s permissive
    per-word-OR content queries: a path fragment's tokens routinely
    include the file extension (``"py"``, ``"md"``, ...), and OR-ing that
    in as its own clause would match *every* file of that type. AND
    keeps a multi-token fragment precise -- exactly the tokens the user
    typed, together -- while a single-token query (the overwhelmingly
    common case: one filename or directory name) behaves identically
    either way.
    """
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return None
    return " AND ".join(f'"{t}"' for t in tokens)


def search_path_projection(
    conn: sqlite3.Connection, query: str, *, limit: int = 25
) -> list[FileRecord]:
    """Path search via the indexed ``path_fts`` table (blueprint section
    11), falling back to ``search_by_substring``'s ``LIKE`` scan when FTS
    finds nothing -- either because the query doesn't tokenize to
    anything (rare) or because it's a mid-word fragment FTS can't match.
    """
    fts_query = _path_fts_query(query)
    if fts_query is not None:
        rows = conn.execute(
            """
            SELECT f.* FROM path_fts
            JOIN files f ON f.id = path_fts.file_id
            WHERE path_fts MATCH ?
            ORDER BY bm25(path_fts), f.path
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
        if rows:
            return [_row_to_file(row) for row in rows]
    return search_by_substring(conn, query, limit=limit)


def insert(conn: sqlite3.Connection, file: FileRecord) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO files (
                id, source_id, path, kind, size, mtime, content_hash, status,
                generation, parser_version, chunker_version, embedding_model_id,
                embedding_text_version, last_indexed_at, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file.id,
                file.source_id,
                file.path,
                file.kind.value,
                file.size,
                file.mtime,
                file.content_hash,
                file.status.value,
                file.generation,
                file.parser_version,
                file.chunker_version,
                file.embedding_model_id,
                file.embedding_text_version,
                file.last_indexed_at,
                file.last_error,
                file.created_at,
                file.updated_at,
            ),
        )
        conn.execute(
            "INSERT INTO path_fts (file_id, path) VALUES (?, ?)",
            (file.id, file.path),
        )


def update_status(
    conn: sqlite3.Connection,
    file_id: str,
    status: FileStatus,
    *,
    updated_at: str,
    size: int | None = None,
    mtime: float | None = None,
    content_hash: str | None = None,
) -> None:
    """Indexing optimization plan, Phase P7: ``size``/``mtime``/
    ``content_hash`` (all optional, ``COALESCE``d against the stored
    value like ``mark_indexed``'s own version-stamp params) let a caller
    that just re-``stat()``/re-hashed an existing file's new content --
    ``indexing/coordinator.py``'s scan loop, transitioning a changed
    file to ``QUEUED`` -- persist that new identity in the same write as
    the status change, rather than leaving the row's stat fields stale
    until (if ever) something else updates them.

    This closes a real, pre-existing bug the plan's Phase P7 end-to-end
    regression test caught: before this fix, only ``status`` was ever
    written here for a changed *existing* file -- its actual new size/
    mtime/content_hash were computed during scanning but never
    persisted anywhere, so ``_process_queue``'s later ``files_repo.get()``
    re-read the *old* stat values and ``mark_indexed`` dutifully wrote
    them straight back. The file therefore always looked "changed"
    again on every subsequent run, forever, regardless of ``content_hash``
    reuse (Phase P3) or targeted scanning (Phase P2) -- both correctly
    detected a real mismatch against genuinely stale stored data.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE files SET status = ?, updated_at = ?, "
            "size = COALESCE(?, size), mtime = COALESCE(?, mtime), "
            "content_hash = COALESCE(?, content_hash) WHERE id = ?",
            (status.value, updated_at, size, mtime, content_hash, file_id),
        )


def mark_indexed(
    conn: sqlite3.Connection,
    file_id: str,
    *,
    size: int,
    mtime: float,
    content_hash: str | None,
    status: FileStatus,
    indexed_at: str,
    parser_version: str | None = None,
    chunker_version: str | None = None,
) -> None:
    """Atomically advances a file to its next generation.

    Phase 1 has no derived-entity tables yet, so "delete old generation,
    insert new" collapses to a single row update -- but it still runs
    inside one transaction so readers never observe a half-updated file.

    ``parser_version``/``chunker_version`` (Search Quality Improvement
    Plan, Phase 12) stamp the reuse identity that actually just produced
    this file's derived rows -- passed by ``IndexCoordinator`` from the
    registered processor's version provider (``None`` for a kind with no
    provider, e.g. code files, or the default raw processor). ``COALESCE``
    leaves the stored value untouched when the caller has nothing to
    stamp, rather than clobbering a real value with ``NULL``.
    """
    with transaction(conn):
        conn.execute(
            """
            UPDATE files
            SET size = ?, mtime = ?, content_hash = ?, status = ?,
                generation = generation + 1, last_indexed_at = ?,
                last_error = NULL, updated_at = ?,
                parser_version = COALESCE(?, parser_version),
                chunker_version = COALESCE(?, chunker_version)
            WHERE id = ?
            """,
            (
                size,
                mtime,
                content_hash,
                status.value,
                indexed_at,
                indexed_at,
                parser_version,
                chunker_version,
                file_id,
            ),
        )


def update_embedding_version(
    conn: sqlite3.Connection,
    file_id: str,
    *,
    embedding_model_id: str,
    embedding_text_version: str,
    updated_at: str,
) -> None:
    """Stamps the embedding half of a file's reuse identity (Search
    Quality Improvement Plan, Phase 12) -- called by
    ``indexing/embedding_indexer.py`` right after a file's vectors are
    actually (re)computed, never speculatively: an embedding step that
    raised/skipped (e.g. ``EmbeddingModelUnavailableError``) must leave
    the previous stamp exactly as it was, so a later run still sees it as
    stale and retries rather than silently accepting a skipped rebuild as
    "done".

    Always run inside the same caller-held transaction as the
    ``embeddings``/``vector_items`` writes it accompanies -- mirrors
    ``embeddings_repo.delete_by_file``/``insert``, never wrapping its own
    transaction, since ``embed_touched_files`` (this function's one
    caller) is itself always invoked from inside one already.
    """
    conn.execute(
        "UPDATE files SET embedding_model_id = ?, embedding_text_version = ?, "
        "updated_at = ? WHERE id = ?",
        (embedding_model_id, embedding_text_version, updated_at, file_id),
    )


def list_missing_embeddings(
    conn: sqlite3.Connection, source_id: str, *, model_id: str
) -> list[FileRecord]:
    """Successfully indexed CODE/DOCUMENT files with no current-model
    vectors at all -- the indexing optimization plan Phase P5 backfill
    target, distinct from ``indexing/incremental.py``'s
    ``ReprocessDecision.EMBEDDINGS_ONLY`` path: that one only fires for a
    file an ordinary scan happens to revisit and finds otherwise
    ``UNCHANGED`` (see ``decide_reprocessing``'s docstring on why a
    stored ``NULL`` embedding stamp is deliberately never treated as
    "stale" there). A file indexed while ``search.semantic`` was off has
    ``embedding_model_id IS NULL`` and perfectly current content/parser/
    chunker stamps -- nothing about it will ever look "changed" or
    "stale" to a normal scan again, so it needs this explicit query
    instead (``ragmonk vectors backfill``) rather than waiting on a scan
    that will never revisit it for this reason.
    """
    rows = conn.execute(
        """
        SELECT * FROM files
        WHERE source_id = ? AND status = ?
          AND kind IN (?, ?)
          AND (embedding_model_id IS NULL OR embedding_model_id != ?)
        ORDER BY path
        """,
        (
            source_id,
            FileStatus.INDEXED.value,
            FileKind.CODE.value,
            FileKind.DOCUMENT.value,
            model_id,
        ),
    ).fetchall()
    return [_row_to_file(row) for row in rows]


def rename(
    conn: sqlite3.Connection,
    file_id: str,
    *,
    new_path: str,
    size: int,
    mtime: float,
    updated_at: str,
) -> None:
    """Reassigns an existing file's path in place -- a detected move/
    rename (see ``indexing/coordinator.py``'s ``_reconcile_renames``) --
    rather than the delete-then-reinsert-as-new a plain path mismatch
    would otherwise cause.

    Every derived row (``document_sections``/``entities``/``embeddings``/
    ``cross_links``) keys off this file's stable ``id``, never its path,
    so keeping the same ``id`` here is what makes chunk/embedding reuse
    for a moved file automatic: nothing downstream needs to know it
    moved. ``content_hash``/``parser_version``/``chunker_version``/
    ``embedding_model_id``/``embedding_text_version`` are deliberately
    left untouched -- a path change alone never invalidates any of them.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE files SET path = ?, size = ?, mtime = ?, updated_at = ? WHERE id = ?",
            (new_path, size, mtime, updated_at, file_id),
        )
        # DELETE + INSERT, not UPDATE, mirroring insert()/delete()'s own
        # path_fts writes -- the safest, most consistent way to change an
        # fts5 row's indexed content.
        conn.execute("DELETE FROM path_fts WHERE file_id = ?", (file_id,))
        conn.execute(
            "INSERT INTO path_fts (file_id, path) VALUES (?, ?)",
            (file_id, new_path),
        )


def mark_failed(conn: sqlite3.Connection, file_id: str, *, error: str, updated_at: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE files SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (FileStatus.FAILED.value, error, updated_at, file_id),
        )


def delete(conn: sqlite3.Connection, file_id: str) -> None:
    """Deletes a file and cascades its derived-content rows.

    ``entities``/``relationships`` (Phase 2), ``documents``/
    ``document_sections`` (Phase 3) and ``cross_links`` (Phase 4) all
    carry a ``file_id`` foreign key into ``files`` (``cross_links``
    indirectly, via ``entities``/``documents``), but were added by later,
    already-applied migrations without an ``ON DELETE CASCADE`` clause --
    so with ``PRAGMA foreign_keys = ON`` (storage/sqlite.py), deleting a
    ``files`` row with surviving derived rows would raise
    ``IntegrityError`` instead of reconciling a source's deleted file
    away. Clearing them here, in the same transaction (``cross_links``
    first, since it references ``entities``/``documents``), keeps that
    reconciliation crash-free without touching those migrations.
    """
    with transaction(conn):
        conn.execute("DELETE FROM index_jobs WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM path_fts WHERE file_id = ?", (file_id,))
        links_repo.delete_by_entity_file(conn, file_id)
        links_repo.delete_by_document_file(conn, file_id)
        conn.execute(
            "DELETE FROM code_fts WHERE entity_id IN "
            "(SELECT id FROM entities WHERE file_id = ?)",
            (file_id,),
        )
        conn.execute("DELETE FROM relationships WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM entities WHERE file_id = ?", (file_id,))
        conn.execute(
            "DELETE FROM document_fts WHERE section_id IN "
            "(SELECT id FROM document_sections WHERE file_id = ?)",
            (file_id,),
        )
        conn.execute("DELETE FROM document_sections WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM documents WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def count_by_status(conn: sqlite3.Connection, source_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM files WHERE source_id = ? GROUP BY status",
        (source_id,),
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}
