from __future__ import annotations

from pathlib import Path

from ragmonk.storage.migrations import MIGRATIONS, apply_migrations, current_version
from ragmonk.storage.schema import CURRENT_SCHEMA_VERSION
from ragmonk.storage.sqlite import connect


def test_apply_migrations_creates_expected_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sources.db")
    try:
        apply_migrations(conn, "sources")
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"sources", "schema_migrations", "metadata"} <= tables
        assert current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        apply_migrations(conn, "knowledge")
        rows = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
        assert rows["n"] == len(MIGRATIONS["knowledge"])
    finally:
        conn.close()


def test_knowledge_db_has_expected_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"files", "index_jobs", "index_errors", "schema_migrations", "metadata"} <= tables
        assert {"entities", "relationships", "code_fts"} <= tables
        assert {"documents", "document_sections", "document_fts"} <= tables
        assert {"cross_links"} <= tables
    finally:
        conn.close()


def test_wal_mode_is_active(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_temp_store_is_memory_and_cache_size_is_configurable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        # 2 = MEMORY (sqlite3's PRAGMA temp_store returns the mode as an int).
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        # Negative cache_size is in KiB, so -65536 is the default 64MB.
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -65536
    finally:
        conn.close()

    custom = connect(tmp_path / "custom.sqlite", cache_size_mb=32)
    try:
        assert custom.execute("PRAGMA cache_size").fetchone()[0] == -32768
    finally:
        custom.close()
