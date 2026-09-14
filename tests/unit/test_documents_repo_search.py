"""``documents_repo.search_fts_projection``: real match-centered snippets
(SQLite FTS5's own ``snippet()``, not a naive body-prefix slice) plus the
page/heading provenance a "ragpilot search --snippets"-style block needs
-- see ``cli/search.py``'s ``_print_document_snippet``.
"""

from __future__ import annotations

from pathlib import Path

from ragpilot.core.models import (
    Document,
    DocumentFormat,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo, files_repo
from ragpilot.storage.sqlite import connect, transaction


def _seed_file(conn) -> None:  # noqa: ANN001 - test helper
    # files_repo.insert manages its own transaction, so this always runs
    # *before* the caller's own `with transaction(conn):` block -- same
    # split as test_document_fts.py's _seed_file/_seed_document.
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/labs/1177646_0076000_1.pdf",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def _seed_document(conn) -> None:  # noqa: ANN001 - test helper
    documents_repo.insert_document(
        conn,
        Document(
            id="d1",
            source_id="s1",
            file_id="f1",
            format=DocumentFormat.PDF,
            title="Lab Report",
            section_count=0,
            paragraph_count=1,
            generation=1,
            created_at="now",
            updated_at="now",
        ),
    )


def test_snippet_is_match_centered_not_a_body_prefix(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            # A long paragraph where the actual query term ("HDL") sits
            # well past index 280 -- the old (body or heading)[:280] slice
            # would have missed it entirely; a real match-centered
            # snippet must not.
            padding = "Other results not relevant to this query. " * 10
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p1",
                    document_id="d1",
                    file_id="f1",
                    text=f"{padding}HDL Cholesterol .......... 51 mg/dL",
                    heading_path=["Lipid Panel"],
                    page_start=2,
                    page_end=2,
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Lab Report",
            )

        rows = documents_repo.search_fts_projection(conn, '"HDL"', snippet_max_tokens=16)

        assert len(rows) == 1
        row = rows[0]
        assert row.snippet is not None
        assert "HDL Cholesterol" in row.snippet
        assert "51 mg/dL" in row.snippet
        # A real match-centered snippet is bounded by max_tokens (16
        # here), not the full ~440-char padded paragraph -- the old
        # (body or heading)[:280] slice would have returned 280 chars of
        # pure padding and missed the actual match entirely.
        assert len(row.snippet) < len(padding)
        assert row.page_start == 2
        assert row.page_end == 2
        assert row.heading_path == ["Lipid Panel"]
    finally:
        conn.close()


def test_snippet_max_tokens_is_clamped_to_fts5_limits(tmp_path: Path) -> None:
    """FTS5's own snippet() raises if max_tokens is outside 1-64 -- a
    caller passing an out-of-range value (shouldn't happen given
    SearchOutputConfig's own validation, but this repository function
    has no way to know that) must not crash the query.
    """
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p1",
                    document_id="d1",
                    file_id="f1",
                    text="HDL Cholesterol .......... 51 mg/dL",
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Lab Report",
            )

        rows_over = documents_repo.search_fts_projection(conn, '"HDL"', snippet_max_tokens=999)
        rows_under = documents_repo.search_fts_projection(conn, '"HDL"', snippet_max_tokens=0)

        assert rows_over[0].snippet is not None
        assert rows_under[0].snippet is not None
    finally:
        conn.close()


def test_page_start_is_none_for_a_format_without_pages(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p1",
                    document_id="d1",
                    file_id="f1",
                    text="HDL Cholesterol reference ranges.",
                    heading_path=["Lipid Panel"],
                    page_start=None,
                    page_end=None,
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Lab Report",
            )

        rows = documents_repo.search_fts_projection(conn, '"HDL"')

        assert rows[0].page_start is None
        assert rows[0].page_end is None
        assert rows[0].heading_path == ["Lipid Panel"]
    finally:
        conn.close()
