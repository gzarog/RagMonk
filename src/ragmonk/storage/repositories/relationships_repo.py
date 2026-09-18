"""CRUD and graph-traversal queries for ``relationships``."""

from __future__ import annotations

import sqlite3

from ragmonk.core.models import Confidence, Relationship, RelationshipType


def _row_to_relationship(row: sqlite3.Row) -> Relationship:
    return Relationship(
        id=row["id"],
        relationship_type=RelationshipType(row["relationship_type"]),
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        target_symbol=row["target_symbol"],
        resolver=row["resolver"],
        confidence=Confidence(row["confidence"]),
        file_id=row["file_id"],
        source_location=row["source_location"],
        evidence=row["evidence"],
        generation=row["generation"],
        created_at=row["created_at"],
    )


def insert(conn: sqlite3.Connection, relationship: Relationship) -> None:
    conn.execute(
        """
        INSERT INTO relationships (
            id, relationship_type, source_entity_id, target_entity_id,
            target_symbol, resolver, confidence, file_id, source_location,
            evidence, generation, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            relationship.id,
            relationship.relationship_type.value,
            relationship.source_entity_id,
            relationship.target_entity_id,
            relationship.target_symbol,
            relationship.resolver,
            relationship.confidence.value,
            relationship.file_id,
            relationship.source_location,
            relationship.evidence,
            relationship.generation,
            relationship.created_at,
        ),
    )


def count_all(conn: sqlite3.Connection) -> int:
    """Total relationship count -- backs ``status --json``'s
    ``relationships_created`` metric (Phase 8)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM relationships").fetchone()
    return int(row["n"])


def list_by_file(conn: sqlite3.Connection, file_id: str) -> list[Relationship]:
    rows = conn.execute(
        "SELECT * FROM relationships WHERE file_id = ? ORDER BY id", (file_id,)
    ).fetchall()
    return [_row_to_relationship(row) for row in rows]


def outgoing(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    relationship_type: RelationshipType | None = None,
    limit: int = 200,
) -> list[Relationship]:
    """Edges where ``entity_id`` is the source, e.g. what it CALLS."""
    if relationship_type is None:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE source_entity_id = ? "
            "ORDER BY relationship_type, id LIMIT ?",
            (entity_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE source_entity_id = ? AND relationship_type = ? "
            "ORDER BY id LIMIT ?",
            (entity_id, relationship_type.value, limit),
        ).fetchall()
    return [_row_to_relationship(row) for row in rows]


def incoming(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    relationship_type: RelationshipType | None = None,
    limit: int = 200,
) -> list[Relationship]:
    """Edges where ``entity_id`` is the resolved target, e.g. its callers."""
    if relationship_type is None:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE target_entity_id = ? "
            "ORDER BY relationship_type, id LIMIT ?",
            (entity_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE target_entity_id = ? AND relationship_type = ? "
            "ORDER BY id LIMIT ?",
            (entity_id, relationship_type.value, limit),
        ).fetchall()
    return [_row_to_relationship(row) for row in rows]


def find_by_target_symbol_prefix(conn: sqlite3.Connection, prefix: str) -> list[Relationship]:
    """Relationships whose ``target_symbol`` starts with ``prefix``.

    Used by ``knowledge/linker.py`` to fetch every ``framework_rules``-
    tagged HTTP route finding (``target_symbol`` = "http_endpoint:METHOD:
    /path") across the whole project in one query, rather than walking
    every entity's outgoing edges to find them.
    """
    rows = conn.execute(
        "SELECT * FROM relationships WHERE target_symbol LIKE ? ORDER BY id",
        (prefix + "%",),
    ).fetchall()
    return [_row_to_relationship(row) for row in rows]


def incoming_by_symbol(
    conn: sqlite3.Connection,
    symbol_name: str,
    *,
    relationship_type: RelationshipType | None = None,
    limit: int = 200,
) -> list[Relationship]:
    """Unresolved edges (``target_entity_id IS NULL``) naming this symbol.

    Kept separate from ``incoming`` because it addresses a name, not an
    entity id -- these are the "low confidence, not dropped" edges the
    resolver stores when it cannot find a defining entity anywhere.
    """
    if relationship_type is None:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE target_entity_id IS NULL AND target_symbol = ? "
            "ORDER BY relationship_type, id LIMIT ?",
            (symbol_name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM relationships WHERE target_entity_id IS NULL AND target_symbol = ? "
            "AND relationship_type = ? ORDER BY id LIMIT ?",
            (symbol_name, relationship_type.value, limit),
        ).fetchall()
    return [_row_to_relationship(row) for row in rows]
