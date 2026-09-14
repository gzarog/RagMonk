"""CRUD for ``documents``, ``document_sections`` and its ``document_fts``
shadow index in a project's ``knowledge.db``.

Regenerating a file's document content is delete-then-insert under one
caller-held transaction (see ``documents/pipeline.py``), mirroring the
generational pattern ``entities_repo``/``files_repo`` established in
Phase 1/2.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ragpilot.core.models import Document, DocumentFormat, Paragraph, Section, SectionKind, Table
from ragpilot.storage.repositories import links_repo


@dataclass(frozen=True)
class DocumentUnit:
    """One ``document_sections`` row, kind-agnostic -- the shape
    ``knowledge/linker.py`` and ``knowledge/evidence.py`` actually need
    (heading/paragraph text, or a table's cells flattened into ``text``
    so a linker match doesn't have to special-case table rows). Not a
    replacement for ``Section``/``Paragraph``/``Table``: those stay the
    Phase 3 storage-facing shapes; this is a read-facing projection.
    """

    id: str
    document_id: str
    file_id: str
    kind: SectionKind
    text: str
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        source_id=row["source_id"],
        file_id=row["file_id"],
        format=DocumentFormat(row["format"]),
        title=row["title"],
        author=row["author"],
        page_count=row["page_count"],
        section_count=row["section_count"],
        paragraph_count=row["paragraph_count"],
        table_count=row["table_count"],
        is_scanned=bool(row["is_scanned"]),
        content_hash=row["content_hash"],
        generation=row["generation"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def delete_by_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Removes a file's previous generation of document content and its
    FTS rows.

    Caller-managed transaction: always run inside the same
    ``with transaction(conn):`` block as the subsequent inserts so a
    reader never observes a document with zero or partial content
    mid-reindex.
    """
    links_repo.delete_by_document_file(conn, file_id)
    conn.execute(
        "DELETE FROM document_fts WHERE section_id IN "
        "(SELECT id FROM document_sections WHERE file_id = ?)",
        (file_id,),
    )
    conn.execute("DELETE FROM document_sections WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM documents WHERE file_id = ?", (file_id,))


def insert_document(conn: sqlite3.Connection, document: Document) -> None:
    conn.execute(
        """
        INSERT INTO documents (
            id, source_id, file_id, format, title, author, page_count,
            section_count, paragraph_count, table_count, is_scanned,
            content_hash, generation, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document.id,
            document.source_id,
            document.file_id,
            document.format.value,
            document.title,
            document.author,
            document.page_count,
            document.section_count,
            document.paragraph_count,
            document.table_count,
            int(document.is_scanned),
            document.content_hash,
            document.generation,
            document.created_at,
            document.updated_at,
        ),
    )


def _insert_row(
    conn: sqlite3.Connection,
    *,
    row_id: str,
    document_id: str,
    file_id: str,
    kind: SectionKind,
    heading_level: int | None,
    text: str,
    heading_path: list[str],
    parent_id: str | None,
    order_index: int,
    page_start: int | None,
    page_end: int | None,
    table_rows: list[list[str]] | None,
    num_rows: int | None,
    num_cols: int | None,
    generation: int,
    created_at: str,
    fts_heading: str,
    fts_body: str,
    doc_title: str,
) -> None:
    conn.execute(
        """
        INSERT INTO document_sections (
            id, document_id, file_id, kind, heading_level, text,
            heading_path, parent_id, order_index, page_start, page_end,
            table_rows, num_rows, num_cols, generation, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            document_id,
            file_id,
            kind.value,
            heading_level,
            text,
            json.dumps(heading_path),
            parent_id,
            order_index,
            page_start,
            page_end,
            json.dumps(table_rows) if table_rows is not None else None,
            num_rows,
            num_cols,
            generation,
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO document_fts (section_id, document_id, heading_text, body, doc_title) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, document_id, fts_heading, fts_body, doc_title),
    )


def insert_section(conn: sqlite3.Connection, section: Section, *, doc_title: str) -> None:
    _insert_row(
        conn,
        row_id=section.id,
        document_id=section.document_id,
        file_id=section.file_id,
        kind=SectionKind.HEADING,
        heading_level=section.heading_level,
        text=section.text,
        heading_path=section.heading_path,
        parent_id=section.parent_id,
        order_index=section.order_index,
        page_start=section.page_start,
        page_end=section.page_end,
        table_rows=None,
        num_rows=None,
        num_cols=None,
        generation=section.generation,
        created_at=section.created_at,
        fts_heading=section.text,
        fts_body="",
        doc_title=doc_title,
    )


def insert_paragraph(conn: sqlite3.Connection, paragraph: Paragraph, *, doc_title: str) -> None:
    _insert_row(
        conn,
        row_id=paragraph.id,
        document_id=paragraph.document_id,
        file_id=paragraph.file_id,
        kind=SectionKind.PARAGRAPH,
        heading_level=None,
        text=paragraph.text,
        heading_path=paragraph.heading_path,
        parent_id=paragraph.parent_id,
        order_index=paragraph.order_index,
        page_start=paragraph.page_start,
        page_end=paragraph.page_end,
        table_rows=None,
        num_rows=None,
        num_cols=None,
        generation=paragraph.generation,
        created_at=paragraph.created_at,
        fts_heading=" > ".join(paragraph.heading_path),
        fts_body=paragraph.text,
        doc_title=doc_title,
    )


def insert_table(conn: sqlite3.Connection, table: Table, *, doc_title: str) -> None:
    flattened = " ".join(cell for row in table.rows for cell in row if cell)
    _insert_row(
        conn,
        row_id=table.id,
        document_id=table.document_id,
        file_id=table.file_id,
        kind=SectionKind.TABLE,
        heading_level=None,
        text="",
        heading_path=table.heading_path,
        parent_id=table.parent_id,
        order_index=table.order_index,
        page_start=table.page_start,
        page_end=table.page_end,
        table_rows=table.rows,
        num_rows=table.num_rows,
        num_cols=table.num_cols,
        generation=table.generation,
        created_at=table.created_at,
        fts_heading=" > ".join(table.heading_path),
        fts_body=flattened,
        doc_title=doc_title,
    )


def _row_to_unit(row: sqlite3.Row) -> DocumentUnit:
    text = row["text"] or ""
    if row["kind"] == SectionKind.TABLE.value and row["table_rows"]:
        cells = json.loads(row["table_rows"])
        text = " ".join(cell for r in cells for cell in r if cell)
    return DocumentUnit(
        id=row["id"],
        document_id=row["document_id"],
        file_id=row["file_id"],
        kind=SectionKind(row["kind"]),
        text=text,
        heading_path=json.loads(row["heading_path"]) if row["heading_path"] else [],
        page_start=row["page_start"],
        page_end=row["page_end"],
    )


def get_unit(conn: sqlite3.Connection, unit_id: str) -> DocumentUnit | None:
    row = conn.execute("SELECT * FROM document_sections WHERE id = ?", (unit_id,)).fetchone()
    return _row_to_unit(row) if row is not None else None


def list_units_by_file(conn: sqlite3.Connection, file_id: str) -> list[DocumentUnit]:
    rows = conn.execute(
        "SELECT * FROM document_sections WHERE file_id = ? ORDER BY order_index", (file_id,)
    ).fetchall()
    return [_row_to_unit(row) for row in rows]


def list_all_units(conn: sqlite3.Connection) -> list[DocumentUnit]:
    """Every heading/paragraph/table unit in this project's
    ``knowledge.db`` -- the "full existing corpus" side of
    ``knowledge/linker.py``'s cross-domain match, same rationale as
    ``entities_repo.list_all``.
    """
    rows = conn.execute(
        "SELECT * FROM document_sections ORDER BY document_id, order_index"
    ).fetchall()
    return [_row_to_unit(row) for row in rows]


def count_all(conn: sqlite3.Connection) -> int:
    """Total document count -- backs ``status --json``'s
    ``documents_processed`` metric (Phase 8)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
    return int(row["n"])


def get_document(conn: sqlite3.Connection, document_id: str) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    return _row_to_document(row) if row is not None else None


def get_document_by_file(conn: sqlite3.Connection, file_id: str) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE file_id = ?", (file_id,)).fetchone()
    return _row_to_document(row) if row is not None else None


def list_by_source(conn: sqlite3.Connection, source_id: str) -> list[Document]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE source_id = ? ORDER BY created_at", (source_id,)
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def list_all(conn: sqlite3.Connection) -> list[Document]:
    rows = conn.execute("SELECT * FROM documents ORDER BY created_at").fetchall()
    return [_row_to_document(row) for row in rows]


def search_fts(conn: sqlite3.Connection, query: str, *, limit: int = 25) -> list[sqlite3.Row]:
    """Direct FTS query, returned as raw rows (``document_id``,
    ``section_id``, ``heading_text``, ``body``, ``doc_title``) -- Phase 5's
    ``search`` command is expected to build on this the same way
    ``entities_repo.search_fts`` seeds Phase 2's graph traversal.
    """
    return conn.execute(
        """
        SELECT document_id, section_id, heading_text, body, doc_title
        FROM document_fts
        WHERE document_fts MATCH ?
        ORDER BY bm25(document_fts)
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()


@dataclass(slots=True, frozen=True)
class DocumentSearchRow:
    """A document-search hit projected straight out of a ``documents``/
    ``document_fts JOIN files`` query (blueprint section 9) -- replaces
    the previous "fetch the document, then a separate ``files_repo.get``
    per row" N+1 pattern in ``retrieval/lexical.py``.
    """

    id: str
    title: str
    path: str
    mtime: float
    snippet: str | None = None
    heading: str | None = None
    fts_rank: int = 0
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = field(default_factory=list)


def search_title_projection(
    conn: sqlite3.Connection, title: str, *, limit: int = 25
) -> list[DocumentSearchRow]:
    """Case-insensitive exact document-title lookup via the indexed
    ``idx_documents_title_nocase`` index, replacing the previous
    ``list_all()`` full-corpus Python scan.
    """
    rows = conn.execute(
        """
        SELECT d.id, d.title, f.path, f.mtime
        FROM documents d
        JOIN files f ON f.id = d.file_id
        WHERE d.title = ? COLLATE NOCASE
        LIMIT ?
        """,
        (title, limit),
    ).fetchall()
    return [
        DocumentSearchRow(id=row["id"], title=row["title"], path=row["path"], mtime=row["mtime"])
        for row in rows
    ]


def search_fts_projection(
    conn: sqlite3.Connection, query: str, *, limit: int = 25, snippet_max_tokens: int = 32
) -> list[DocumentSearchRow]:
    """``snippet_max_tokens`` is clamped to FTS5's own ``snippet()`` hard
    limit (1-64 tokens) rather than trusted as-is -- a caller passing a
    ``search.output.snippet_max_tokens`` config value already validated
    to that same range (``core/config.py``'s ``SearchOutputConfig``)
    never hits the clamp, but this repository function has no way to
    know the value reaching it was validated, so it re-enforces the one
    constraint that would otherwise make the query itself raise.

    ``snippet(document_fts, -1, ...)``: column ``-1`` lets FTS5 pick
    whichever indexed column (``heading_text``/``body``/``doc_title``)
    actually matched, rather than assuming ``body`` -- a heading-only or
    title-only hit still gets a real match-centered excerpt instead of an
    empty one. No highlight markers (``''``/``''`` start/end) -- this is
    plain extracted text, not markup, so it renders identically in a
    terminal, ``--json``, or an MCP tool response.
    """
    clamped_max_tokens = max(1, min(64, snippet_max_tokens))
    rows = conn.execute(
        """
        SELECT df.document_id, df.section_id, df.heading_text, df.body,
               df.doc_title, d.title AS document_title, f.path, f.mtime,
               ds.page_start, ds.page_end, ds.heading_path,
               bm25(document_fts) AS rank,
               snippet(document_fts, -1, '', '', '...', ?) AS match_snippet
        FROM document_fts df
        JOIN documents d ON d.id = df.document_id
        JOIN files f ON f.id = d.file_id
        JOIN document_sections ds ON ds.id = df.section_id
        WHERE document_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (clamped_max_tokens, query, limit),
    ).fetchall()
    results: list[DocumentSearchRow] = []
    for rank, row in enumerate(rows):
        heading = row["heading_text"] or ""
        body = row["body"] or ""
        title = row["doc_title"] or row["document_title"] or row["path"]
        match_snippet = (row["match_snippet"] or "").strip()
        results.append(
            DocumentSearchRow(
                id=row["section_id"] or row["document_id"],
                title=title,
                path=row["path"],
                mtime=row["mtime"],
                snippet=match_snippet or (body or heading)[:280] or None,
                heading=heading or None,
                fts_rank=rank,
                page_start=row["page_start"],
                page_end=row["page_end"],
                heading_path=json.loads(row["heading_path"]) if row["heading_path"] else [],
            )
        )
    return results
