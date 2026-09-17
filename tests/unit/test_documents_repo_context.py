"""``documents_repo.get_chunk_neighbors``: the Search Quality Improvement
Plan Phase 9 lookup a matched chunk's context expansion is built on --
its nearest parent heading and previous/next siblings, derived purely
from the existing ``parent_id``/``order_index`` columns (no new storage).
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
    Section,
)
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo
from ragpilot.storage.repositories.files_repo import insert as insert_file
from ragpilot.storage.sqlite import connect, transaction


def _seed_file(conn) -> None:  # noqa: ANN001 - test helper
    insert_file(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/docs/report.md",
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
            format=DocumentFormat.MARKDOWN,
            title="Report",
            section_count=1,
            paragraph_count=3,
            generation=1,
            created_at="now",
            updated_at="now",
        ),
    )


def _seed_multi_paragraph_section(conn) -> None:  # noqa: ANN001 - test helper
    """One heading with three paragraph children (``p1``/``p2``/``p3``,
    ``order_index`` 1/2/3 -- the heading itself is ``order_index=0``) so
    ``p2`` has both a previous and a next sibling, while ``p1``/``p3``
    each have only one side.
    """
    documents_repo.insert_section(
        conn,
        Section(
            id="h1",
            document_id="d1",
            file_id="f1",
            heading_level=1,
            text="Results",
            heading_path=["Results"],
            parent_id=None,
            order_index=0,
            generation=1,
            created_at="now",
        ),
        doc_title="Report",
    )
    for index, (chunk_id, text) in enumerate(
        [("p1", "First paragraph."), ("p2", "Second paragraph."), ("p3", "Third paragraph.")],
        start=1,
    ):
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id=chunk_id,
                document_id="d1",
                file_id="f1",
                text=text,
                heading_path=["Results"],
                parent_id="h1",
                order_index=index,
                generation=1,
                created_at="now",
            ),
            doc_title="Report",
        )


def test_mid_document_chunk_has_both_neighbors(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p2", previous_chunks=1, next_chunks=1
        )

        assert neighbors.parent_heading is not None
        assert neighbors.parent_heading.id == "h1"
        assert [u.id for u in neighbors.previous] == ["p1"]
        assert [u.id for u in neighbors.next] == ["p3"]
    finally:
        conn.close()


def test_first_chunk_has_no_previous_sibling(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p1", previous_chunks=1, next_chunks=1
        )

        assert neighbors.previous == []
        assert [u.id for u in neighbors.next] == ["p2"]
    finally:
        conn.close()


def test_last_chunk_has_no_next_sibling(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p3", previous_chunks=1, next_chunks=1
        )

        assert [u.id for u in neighbors.previous] == ["p2"]
        assert neighbors.next == []
    finally:
        conn.close()


def test_multiple_previous_and_next_chunks_are_nearest_first_and_chronological(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p2", previous_chunks=5, next_chunks=5
        )

        # Only one real sibling exists on each side even though 5 were
        # requested -- never an error, just everything that actually
        # exists, in document order.
        assert [u.id for u in neighbors.previous] == ["p1"]
        assert [u.id for u in neighbors.next] == ["p3"]
    finally:
        conn.close()


def test_zero_previous_and_next_returns_no_siblings(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p2", previous_chunks=0, next_chunks=0
        )

        assert neighbors.previous == []
        assert neighbors.next == []
        # Parent heading defaults to included regardless of sibling counts.
        assert neighbors.parent_heading is not None
    finally:
        conn.close()


def test_include_parent_heading_false_omits_it(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "p2", previous_chunks=1, next_chunks=1, include_parent_heading=False
        )

        assert neighbors.parent_heading is None
    finally:
        conn.close()


def test_top_level_chunk_with_no_parent_has_no_parent_heading(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="top",
                    document_id="d1",
                    file_id="f1",
                    text="No heading above this one.",
                    parent_id=None,
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Report",
            )

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "top", previous_chunks=1, next_chunks=1
        )

        assert neighbors.parent_heading is None
        assert neighbors.previous == []
        assert neighbors.next == []
    finally:
        conn.close()


def test_unknown_unit_id_degrades_to_empty_neighbors(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_file(conn)
        with transaction(conn):
            _seed_document(conn)
            _seed_multi_paragraph_section(conn)

        neighbors = documents_repo.get_chunk_neighbors(
            conn, "does-not-exist", previous_chunks=1, next_chunks=1
        )

        assert neighbors == documents_repo.ChunkNeighbors(
            parent_heading=None, previous=[], next=[]
        )
    finally:
        conn.close()
