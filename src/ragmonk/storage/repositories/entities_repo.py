"""CRUD for ``entities`` and its ``code_fts`` shadow index in a project's
``knowledge.db``.

Regenerating a file's entities is delete-then-insert under one caller-held
transaction (see ``code/processor.py``), mirroring the generational pattern
``files_repo.mark_indexed`` established in Phase 1.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ragmonk.core.models import Entity, EntityType
from ragmonk.storage.repositories import links_repo

# Mirrors ``knowledge/linker.py``'s ``match_alias``/``_MIN_ALIAS_SEGMENTS``:
# the qualified name's final two dotted segments (its class-and-member
# form), skipped below this many segments so the alias never just
# duplicates the qualified name itself. Kept as its own copy here (rather
# than importing from ``knowledge/linker.py`` or ``retrieval/lexical.py``)
# following this project's existing precedent of a small, independently
# documented duplicate over a new cross-module dependency for one
# three-line rule.
_MIN_ALIAS_SEGMENTS = 3


def compute_alias(qualified_name: str) -> str | None:
    """The indexed ``entities.alias`` value for ``qualified_name``, or
    ``None`` when it has too few dotted segments to be worth aliasing
    (see ``_MIN_ALIAS_SEGMENTS``).
    """
    segments = qualified_name.split(".")
    if len(segments) < _MIN_ALIAS_SEGMENTS:
        return None
    return ".".join(segments[-2:])


@dataclass(slots=True, frozen=True)
class EntitySearchRow:
    """A lexical-search hit projected straight out of a ``entities JOIN
    files`` query (blueprint section 9): only the fields ``retrieval/
    lexical.py`` actually renders, with the file's ``path``/``mtime``
    already joined in. Replaces the previous "fetch a full ``Entity``,
    then a separate ``files_repo.get`` per row" N+1 pattern -- see
    ``search_exact_projection``/``search_alias_projection``/
    ``search_fts_projection`` below, the sole producers of this type.
    """

    id: str
    name: str
    qualified_name: str
    kind: EntityType
    signature: str | None
    start_line: int
    end_line: int
    path: str
    mtime: float
    fts_rank: int = 0
    # Search Quality Improvement Plan, Phase 8: the raw ``bm25()`` value
    # (lower/more negative is a better match), preserved alongside
    # ``fts_rank``'s ordinal position -- see ``DocumentSearchRow.
    # bm25_score``'s docstring for why the two are kept separate.
    bm25_score: float | None = None


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        source_id=row["source_id"],
        file_id=row["file_id"],
        kind=EntityType(row["kind"]),
        name=row["name"],
        qualified_name=row["qualified_name"],
        language=row["language"],
        parent_id=row["parent_id"],
        signature=row["signature"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        start_col=row["start_col"],
        end_col=row["end_col"],
        generation=row["generation"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def delete_by_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Removes a file's previous generation of entities and their FTS rows.

    Caller-managed transaction: this is always run inside the same
    ``with transaction(conn):`` block as the subsequent inserts so a reader
    never observes a file with zero or partial entities mid-reindex.
    """
    links_repo.delete_by_entity_file(conn, file_id)
    conn.execute(
        "DELETE FROM code_fts WHERE entity_id IN (SELECT id FROM entities WHERE file_id = ?)",
        (file_id,),
    )
    conn.execute("DELETE FROM relationships WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM entities WHERE file_id = ?", (file_id,))


def insert(conn: sqlite3.Connection, entity: Entity, *, snippet: str) -> None:
    conn.execute(
        """
        INSERT INTO entities (
            id, source_id, file_id, kind, name, qualified_name, language,
            parent_id, signature, start_line, end_line, start_col, end_col,
            generation, created_at, updated_at, alias
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity.id,
            entity.source_id,
            entity.file_id,
            entity.kind.value,
            entity.name,
            entity.qualified_name,
            entity.language,
            entity.parent_id,
            entity.signature,
            entity.start_line,
            entity.end_line,
            entity.start_col,
            entity.end_col,
            entity.generation,
            entity.created_at,
            entity.updated_at,
            compute_alias(entity.qualified_name),
        ),
    )
    conn.execute(
        "INSERT INTO code_fts (entity_id, name, qualified_name, snippet) VALUES (?, ?, ?, ?)",
        (entity.id, entity.name, entity.qualified_name, snippet),
    )


def get(conn: sqlite3.Connection, entity_id: str) -> Entity | None:
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return _row_to_entity(row) if row is not None else None


def count_all(conn: sqlite3.Connection) -> int:
    """Total entity count -- backs ``status --json``'s ``symbols_created``
    metric (Phase 8). A plain ``COUNT(*)`` rather than ``len(list_all())``
    so a large project doesn't pay to materialize every row just to size it.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()
    return int(row["n"])


def list_all(conn: sqlite3.Connection) -> list[Entity]:
    """Every entity in this project's ``knowledge.db`` -- the "full
    existing corpus" side of ``knowledge/linker.py``'s cross-domain match
    (one knowledge.db always holds exactly one source's data, see
    ``core/paths.py``'s one-project-per-source layout, so no further
    scoping is needed here).
    """
    rows = conn.execute("SELECT * FROM entities ORDER BY file_id, start_line").fetchall()
    return [_row_to_entity(row) for row in rows]


def list_by_file(conn: sqlite3.Connection, file_id: str) -> list[Entity]:
    rows = conn.execute(
        "SELECT * FROM entities WHERE file_id = ? ORDER BY start_line", (file_id,)
    ).fetchall()
    return [_row_to_entity(row) for row in rows]


def find_by_qualified_name(conn: sqlite3.Connection, qualified_name: str) -> list[Entity]:
    rows = conn.execute(
        "SELECT * FROM entities WHERE qualified_name = ? ORDER BY qualified_name, file_id, "
        "start_line",
        (qualified_name,),
    ).fetchall()
    return [_row_to_entity(row) for row in rows]


def find_by_name(conn: sqlite3.Connection, name: str) -> list[Entity]:
    """Bare-name lookup, e.g. the last dotted segment of a call target.

    Used both by the resolver (cross-file, name-only fallback) and by the
    ``ragmonk symbol`` CLI command (a user rarely types a fully qualified
    name). Ordered deterministically by qualified name so repeated runs and
    ``--json`` output are stable.
    """
    rows = conn.execute(
        "SELECT * FROM entities WHERE name = ? ORDER BY qualified_name, file_id, start_line",
        (name,),
    ).fetchall()
    return [_row_to_entity(row) for row in rows]


def search(conn: sqlite3.Connection, name_or_qualified_name: str) -> list[Entity]:
    """Symbol lookup by exact name OR exact qualified name, deduplicated."""
    rows = conn.execute(
        "SELECT * FROM entities WHERE name = ? OR qualified_name = ? "
        "ORDER BY qualified_name, file_id, start_line",
        (name_or_qualified_name, name_or_qualified_name),
    ).fetchall()
    return [_row_to_entity(row) for row in rows]


def search_fts(conn: sqlite3.Connection, query: str, *, limit: int = 25) -> list[Entity]:
    rows = conn.execute(
        """
        SELECT e.* FROM code_fts
        JOIN entities e ON e.id = code_fts.entity_id
        WHERE code_fts MATCH ?
        ORDER BY bm25(code_fts)
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [_row_to_entity(row) for row in rows]


def _row_to_search_row(
    row: sqlite3.Row, *, fts_rank: int = 0, bm25_score: float | None = None
) -> EntitySearchRow:
    return EntitySearchRow(
        id=row["id"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        kind=EntityType(row["kind"]),
        signature=row["signature"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        path=row["path"],
        mtime=row["mtime"],
        fts_rank=fts_rank,
        bm25_score=bm25_score,
    )


_PROJECTION_SELECT = """
    SELECT e.id, e.name, e.qualified_name, e.kind, e.signature,
           e.start_line, e.end_line, f.path, f.mtime
    FROM entities e
    JOIN files f ON f.id = e.file_id
"""


def search_exact_projection(
    conn: sqlite3.Connection, name_or_qualified_name: str
) -> list[EntitySearchRow]:
    """``search()``'s exact name/qualified-name lookup, projected with its
    file already joined in -- see ``EntitySearchRow``.
    """
    rows = conn.execute(
        _PROJECTION_SELECT + "WHERE e.name = ? OR e.qualified_name = ? "
        "ORDER BY e.qualified_name, e.file_id, e.start_line",
        (name_or_qualified_name, name_or_qualified_name),
    ).fetchall()
    return [_row_to_search_row(row) for row in rows]


def search_alias_projection(
    conn: sqlite3.Connection, alias: str, *, limit: int = 25
) -> list[EntitySearchRow]:
    """Indexed ``entities.alias`` lookup (blueprint section 7), replacing
    the previous ``list_all()`` full-corpus Python scan.
    """
    rows = conn.execute(
        _PROJECTION_SELECT + "WHERE e.alias = ? "
        "ORDER BY e.qualified_name, e.file_id, e.start_line LIMIT ?",
        (alias, limit),
    ).fetchall()
    return [_row_to_search_row(row) for row in rows]


def search_fts_projection(
    conn: sqlite3.Connection, query: str, *, limit: int = 25
) -> list[EntitySearchRow]:
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.qualified_name, e.kind, e.signature,
               e.start_line, e.end_line, f.path, f.mtime, bm25(code_fts) AS rank
        FROM code_fts
        JOIN entities e ON e.id = code_fts.entity_id
        JOIN files f ON f.id = e.file_id
        WHERE code_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [
        _row_to_search_row(row, fts_rank=rank, bm25_score=row["rank"])
        for rank, row in enumerate(rows)
    ]
