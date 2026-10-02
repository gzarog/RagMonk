"""Ordered, idempotent schema migrations.

Each migration is a fixed set of DDL statements recorded by version in
``schema_migrations``. Re-running ``apply_migrations`` against an
up-to-date database is a no-op -- this is what makes startup safe to call
unconditionally and what the migration-idempotency test asserts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ragmonk.storage import schema
from ragmonk.storage.sqlite import transaction

DatabaseKind = Literal["sources", "knowledge"]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    # A table-rebuild migration (new table, copy, drop, rename) must run
    # with ``foreign_keys`` OFF -- SQLite's documented procedure: dropping
    # a referenced table would otherwise cascade/violate on its children.
    # The pragma is a no-op inside a transaction, so it is toggled around
    # it, and ``foreign_key_check`` must come back clean before commit.
    rebuilds_tables: bool = False


MIGRATIONS: dict[DatabaseKind, tuple[Migration, ...]] = {
    "sources": (
        Migration(1, "initial_schema", schema.SOURCES_DB_V1),
        Migration(2, "source_status", schema.SOURCES_DB_V2),
    ),
    "knowledge": (
        Migration(1, "initial_schema", schema.KNOWLEDGE_DB_V1),
        Migration(2, "code_intelligence", schema.KNOWLEDGE_DB_V2),
        Migration(3, "document_pipeline", schema.KNOWLEDGE_DB_V3),
        Migration(4, "cross_domain_linking", schema.KNOWLEDGE_DB_V4),
        Migration(5, "embeddings", schema.KNOWLEDGE_DB_V5),
        Migration(6, "pdf_conversion_cache", schema.KNOWLEDGE_DB_V6),
        Migration(7, "entity_alias_and_title_index", schema.KNOWLEDGE_DB_V7),
        Migration(8, "path_fts", schema.KNOWLEDGE_DB_V8),
        Migration(9, "vector_items", schema.KNOWLEDGE_DB_V9),
        Migration(10, "native_docling_document_cache", schema.KNOWLEDGE_DB_V10),
        Migration(11, "raw_search_embedding_text", schema.KNOWLEDGE_DB_V11),
        Migration(12, "table_caption_column", schema.KNOWLEDGE_DB_V12),
        Migration(13, "pdf_conversion_cache_ocr_key", schema.KNOWLEDGE_DB_V13),
        Migration(14, "file_reuse_identity_stamps", schema.KNOWLEDGE_DB_V14),
        Migration(15, "embedding_cache", schema.KNOWLEDGE_DB_V15),
        Migration(
            16,
            "document_attachment_provenance",
            schema.KNOWLEDGE_DB_V16,
            rebuilds_tables=True,
        ),
    ),
}


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def apply_migrations(conn: sqlite3.Connection, kind: DatabaseKind) -> None:
    applied = _applied_versions(conn)
    for migration in MIGRATIONS[kind]:
        if migration.version in applied:
            continue
        fk_was_on = False
        if migration.rebuilds_tables:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
            fk_was_on = bool(row[0]) if row is not None else False
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with transaction(conn):
                for statement in migration.statements:
                    conn.execute(statement)
                if migration.rebuilds_tables:
                    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise sqlite3.IntegrityError(
                            f"migration {migration.version} ({migration.name}) left "
                            f"{len(violations)} foreign key violation(s)"
                        )
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, datetime.now(UTC).isoformat()),
                )
        finally:
            if fk_was_on:
                conn.execute("PRAGMA foreign_keys = ON")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    value = row["v"] if row is not None else None
    return int(value) if value is not None else 0
