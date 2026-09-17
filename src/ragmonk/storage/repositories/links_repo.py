"""CRUD for ``cross_links`` (Phase 4 cross-domain facts) in a project's
``knowledge.db``.

Unlike ``entities_repo``/``documents_repo``, there is no "regenerate this
file's rows" bulk-insert entry point here: cross-domain links are
produced one at a time by ``knowledge/linker.py`` (auto-discovered) or
``cli/link.py`` (explicit, user-defined). ``delete_by_entity_file``/
``delete_by_document_file`` give the two owning repositories (and
``files_repo.delete``) a way to cascade-clean stale links before their
own generational delete, mirroring the pattern they already use for
``code_fts``/``document_fts``.
"""

from __future__ import annotations

import sqlite3

from ragmonk.core.models import Confidence, CrossLink, RelationshipType


def _row_to_cross_link(row: sqlite3.Row) -> CrossLink:
    return CrossLink(
        id=row["id"],
        link_type=RelationshipType(row["link_type"]),
        entity_id=row["entity_id"],
        document_id=row["document_id"],
        section_id=row["section_id"],
        resolver=row["resolver"],
        confidence=Confidence(row["confidence"]),
        evidence=row["evidence"],
        created_at=row["created_at"],
    )


def delete_by_entity_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Removes links pinned to any entity of ``file_id``.

    Entity ids are not stable across a regeneration (``code_processor``
    deletes and reinserts every entity of a changed file with fresh
    uuids -- see ``entities_repo.delete_by_file``), so a link (auto or
    user-defined) pinned to one of those entity ids necessarily goes
    stale the moment its file is re-indexed with different content. This
    call is what turns that "would otherwise dangle / violate the
    ``entity_id`` foreign key" state into a clean deletion instead,
    consistent with Phase 1-3's generational-deletion pattern. Caller-
    managed transaction, always run before deleting the entities
    themselves.
    """
    conn.execute(
        "DELETE FROM cross_links WHERE entity_id IN (SELECT id FROM entities WHERE file_id = ?)",
        (file_id,),
    )


def delete_by_document_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Removes links pinned to the document (and its sections) of
    ``file_id``, for the same reason and in the same "run before the
    owning delete" style as ``delete_by_entity_file``.
    """
    conn.execute(
        "DELETE FROM cross_links WHERE document_id IN (SELECT id FROM documents WHERE file_id = ?)",
        (file_id,),
    )


def insert(conn: sqlite3.Connection, link: CrossLink) -> bool:
    """Inserts one link, ignoring an exact duplicate (same entity,
    document, section and resolver -- the ``UNIQUE`` constraint in
    ``KNOWLEDGE_DB_V4``) rather than raising. This is what makes the
    linker's per-run candidate generation idempotent: re-finding the same
    evidence on a later run (e.g. because both sides of a pair were
    touched in the same run and matched from each direction) is a no-op,
    not a duplicate row. Returns whether a row was actually inserted.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO cross_links (
            id, link_type, entity_id, document_id, section_id, resolver,
            confidence, evidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            link.id,
            link.link_type.value,
            link.entity_id,
            link.document_id,
            link.section_id,
            link.resolver,
            link.confidence.value,
            link.evidence,
            link.created_at,
        ),
    )
    return cursor.rowcount > 0


def get(conn: sqlite3.Connection, link_id: str) -> CrossLink | None:
    row = conn.execute("SELECT * FROM cross_links WHERE id = ?", (link_id,)).fetchone()
    return _row_to_cross_link(row) if row is not None else None


def list_all(conn: sqlite3.Connection) -> list[CrossLink]:
    rows = conn.execute("SELECT * FROM cross_links ORDER BY created_at, id").fetchall()
    return [_row_to_cross_link(row) for row in rows]


def list_by_entity(conn: sqlite3.Connection, entity_id: str) -> list[CrossLink]:
    rows = conn.execute(
        "SELECT * FROM cross_links WHERE entity_id = ? ORDER BY created_at, id", (entity_id,)
    ).fetchall()
    return [_row_to_cross_link(row) for row in rows]


def list_by_document(conn: sqlite3.Connection, document_id: str) -> list[CrossLink]:
    rows = conn.execute(
        "SELECT * FROM cross_links WHERE document_id = ? ORDER BY created_at, id", (document_id,)
    ).fetchall()
    return [_row_to_cross_link(row) for row in rows]


def delete(conn: sqlite3.Connection, link_id: str) -> bool:
    """Deletes one link by id. Returns whether a row existed to delete --
    ``cli/link.py``'s ``remove`` command uses this to report "no such
    link" rather than silently succeeding on an unknown id.
    """
    cursor = conn.execute("DELETE FROM cross_links WHERE id = ?", (link_id,))
    return cursor.rowcount > 0
