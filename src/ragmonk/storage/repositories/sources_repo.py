"""CRUD for the ``sources`` table in the global ``sources.db``."""

from __future__ import annotations

import json
import sqlite3

from ragmonk.core.models import IndexingMode, Source, SourceStatus, SourceType
from ragmonk.storage.sqlite import transaction


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        path=row["path"],
        source_type=SourceType(row["source_type"]),
        enabled=bool(row["enabled"]),
        indexing_mode=IndexingMode(row["indexing_mode"]),
        include_patterns=json.loads(row["include_patterns"]),
        exclude_patterns=json.loads(row["exclude_patterns"]),
        last_scan_at=row["last_scan_at"],
        last_error=row["last_error"],
        fingerprint=row["fingerprint"],
        status=SourceStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create(conn: sqlite3.Connection, source: Source) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO sources (
                id, path, source_type, enabled, indexing_mode,
                include_patterns, exclude_patterns, last_scan_at,
                last_error, fingerprint, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.path,
                source.source_type.value,
                int(source.enabled),
                source.indexing_mode.value,
                json.dumps(source.include_patterns),
                json.dumps(source.exclude_patterns),
                source.last_scan_at,
                source.last_error,
                source.fingerprint,
                source.status.value,
                source.created_at,
                source.updated_at,
            ),
        )


def get(conn: sqlite3.Connection, source_id: str) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_source(row) if row is not None else None


def get_by_path(conn: sqlite3.Connection, path: str) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    return _row_to_source(row) if row is not None else None


def list_all(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Source]:
    query = "SELECT * FROM sources"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY created_at"
    rows = conn.execute(query).fetchall()
    return [_row_to_source(row) for row in rows]


def set_enabled(
    conn: sqlite3.Connection, source_id: str, enabled: bool, *, updated_at: str
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), updated_at, source_id),
        )


def update_scan_result(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    last_scan_at: str,
    last_error: str | None,
    status: SourceStatus,
    updated_at: str,
) -> None:
    """Records the outcome of one scan attempt, ``status`` included so a
    single write carries both the per-file failure summary (``last_error``
    when the source is reachable) and the offline/online transition --
    see ``indexing/runner.py``, the only caller.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE sources SET last_scan_at = ?, last_error = ?, status = ?, "
            "updated_at = ? WHERE id = ?",
            (last_scan_at, last_error, status.value, updated_at, source_id),
        )


def delete(conn: sqlite3.Connection, source_id: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
