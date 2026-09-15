"""CRUD for ``document_conversion_cache`` in a project's ``knowledge.db``.

Caches Docling's real PDF-pipeline output -- the native ``DoclingDocument``
it produces, serialized whole (see ``documents/docling_adapter.py``'s
module docstring) -- so re-indexing an unchanged PDF never re-runs
Docling's real, expensive layout/table-structure ML pipeline. Keyed by
content hash (and, since Phase 5, ``ocr_used``) rather than file id/path,
and never actively pruned -- purely derived, disposable state, mirroring
``embeddings_repo``'s own "never actively GC'd" precedent.

Search Quality Improvement Plan, Phase 5: a PDF's *plain* conversion and
its *OCR'd* conversion are genuinely different documents, so a lookup that
ignored which one it needed could hand a caller wanting OCR text back a
plain-pipeline row (or vice versa) whenever both happen to exist for the
same content hash. ``ocr_used`` (``"off"``/``"on"`` -- see
``docling_adapter``'s ``_OCR_NOT_APPLIED``/``_OCR_APPLIED``) makes that
impossible: it is part of the row's key, not just a stored attribute, so
each of a content hash's up-to-two rows is looked up (and upserted)
independently.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedConversion:
    content_hash: str
    ocr_used: str
    serialized_document: str
    serialization_format: str
    page_count: int | None
    parser_version: str


def get(
    conn: sqlite3.Connection, content_hash: str, *, ocr_used: str, cache_version: int
) -> CachedConversion | None:
    """Returns the cached serialized ``DoclingDocument`` for
    ``content_hash``/``ocr_used``, or ``None`` on a miss. A row whose
    ``cache_version`` no longer matches the caller's (``docling_adapter``'s
    serialization scheme changed since it was written -- including a row
    written before Phase 1B, back when this table cached Markdown text
    instead) is treated as a miss too, not returned as if still valid --
    the caller would otherwise try to deserialize bytes that no longer
    match how it interprets the result.
    """
    row = conn.execute(
        "SELECT content_hash, ocr_used, serialized_document, serialization_format, "
        "page_count, parser_version FROM document_conversion_cache "
        "WHERE content_hash = ? AND ocr_used = ? AND cache_version = ?",
        (content_hash, ocr_used, cache_version),
    ).fetchone()
    if row is None:
        return None
    return CachedConversion(
        content_hash=row["content_hash"],
        ocr_used=row["ocr_used"],
        serialized_document=row["serialized_document"],
        serialization_format=row["serialization_format"],
        page_count=row["page_count"],
        parser_version=row["parser_version"],
    )


def put(
    conn: sqlite3.Connection,
    cached: CachedConversion,
    *,
    cache_version: int,
    created_at: str,
) -> None:
    """Upserts the cache row for ``cached.content_hash``/``cached.ocr_used``.
    A conflicting existing row (a stale ``cache_version``, or any other
    inconsistency) is simply overwritten -- always run inside the caller's
    own ``with transaction(conn):`` block, mirroring every other repo
    write in this project; this function never commits itself.
    """
    conn.execute(
        """
        INSERT INTO document_conversion_cache (
            content_hash, ocr_used, serialized_document, serialization_format,
            page_count, parser_version, cache_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_hash, ocr_used) DO UPDATE SET
            serialized_document = excluded.serialized_document,
            serialization_format = excluded.serialization_format,
            page_count = excluded.page_count,
            parser_version = excluded.parser_version,
            cache_version = excluded.cache_version,
            created_at = excluded.created_at
        """,
        (
            cached.content_hash,
            cached.ocr_used,
            cached.serialized_document,
            cached.serialization_format,
            cached.page_count,
            cached.parser_version,
            cache_version,
            created_at,
        ),
    )
