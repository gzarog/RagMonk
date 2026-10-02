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


def test_document_attachment_provenance_migration_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    """EML attachment V1: migration 16 rebuilds ``documents`` (dropping
    ``UNIQUE(file_id)``) without losing rows, FK integrity or the need for
    a reindex -- old documents read back with null attachment fields.
    """
    from ragmonk.storage.repositories import documents_repo

    conn = connect(tmp_path / "knowledge.db")
    full = MIGRATIONS["knowledge"]
    MIGRATIONS["knowledge"] = tuple(m for m in full if m.version < 16)
    try:
        apply_migrations(conn, "knowledge")
    finally:
        MIGRATIONS["knowledge"] = full
    conn.execute(
        "INSERT INTO files (id, source_id, path, kind, size, mtime, status, created_at, "
        "updated_at) VALUES ('f1', 's1', '/a.eml', 'document', 1, 1, 'indexed', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO documents (id, source_id, file_id, format, title, generation, "
        "created_at, updated_at) VALUES ('d1', 's1', 'f1', 'eml', 'Old', 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO document_sections (id, document_id, file_id, kind, text, heading_path, "
        "order_index, generation, created_at) "
        "VALUES ('u1', 'd1', 'f1', 'paragraph', 't', '[]', 0, 1, 'x')"
    )
    conn.commit()

    apply_migrations(conn, "knowledge")

    assert current_version(conn) == 16
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    document = documents_repo.get_document(conn, "d1")
    assert document is not None and document.title == "Old"
    assert document.parent_document_id is None
    assert document.attachment_name is None
    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'documents'")
    }
    assert {
        "idx_documents_source",
        "idx_documents_title_nocase",
        "idx_documents_parent",
        "idx_documents_file_generation",
    } <= indexes
    # A second document sharing the file id (an attachment child) is allowed now.
    conn.execute(
        "INSERT INTO documents (id, source_id, file_id, format, generation, created_at, "
        "updated_at, parent_document_id, attachment_name) "
        "VALUES ('d2', 's1', 'f1', 'txt', 1, 'x', 'x', 'd1', 'a.txt')"
    )
    assert [d.id for d in documents_repo.list_attachments(conn, "d1")] == ["d2"]
