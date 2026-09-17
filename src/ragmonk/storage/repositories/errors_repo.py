"""CRUD for the ``index_errors`` table in a project's ``knowledge.db``."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from ragmonk.core.models import IndexErrorRecord
from ragmonk.storage.sqlite import transaction


def record(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    file_id: str | None,
    path: str | None,
    error_code: str,
    error_message: str,
) -> str:
    error_id = uuid.uuid4().hex
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO index_errors (
                id, source_id, file_id, path, error_code, error_message, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                error_id,
                source_id,
                file_id,
                path,
                error_code,
                error_message,
                datetime.now(UTC).isoformat(),
            ),
        )
    return error_id


def list_for_source(conn: sqlite3.Connection, source_id: str) -> list[IndexErrorRecord]:
    rows = conn.execute(
        "SELECT * FROM index_errors WHERE source_id = ? ORDER BY occurred_at DESC", (source_id,)
    ).fetchall()
    return [
        IndexErrorRecord(
            id=row["id"],
            source_id=row["source_id"],
            file_id=row["file_id"],
            path=row["path"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            occurred_at=row["occurred_at"],
        )
        for row in rows
    ]
