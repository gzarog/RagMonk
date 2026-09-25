"""CRUD for ``documents``, ``document_sections`` and its ``document_fts``
shadow index in a project's ``knowledge.db``.

Regenerating a file's document content is delete-then-insert under one
caller-held transaction (see ``documents/pipeline.py``), mirroring the
generational pattern ``entities_repo``/``files_repo`` established in
Phase 1/2.

Search Quality Improvement Plan, Phase 3: each ``insert_section``/
``insert_paragraph``/``insert_table`` now takes an explicit
``search_text`` -- what actually gets written to ``document_fts``'s
indexed body column, in place of the raw unit text these functions
previously derived it from themselves. The real caller
(``documents/pipeline.py``) always passes ``chunker.Chunk.search_text``
(document title + heading path + raw text -- see ``documents/
chunker.py``); ``search_text`` defaults to ``None`` here only so the
project's many existing direct-insert call sites (unit tests, the
synthetic-corpus benchmark) that don't care about this field keep their
previous FTS body content unchanged rather than being forced to pass it.
``embedding_text`` (``Chunk.contextual_text``) is stored verbatim on
``document_sections`` for the same reason ``embedding_indexer.py`` needs
it: that module reads already-persisted rows, not live ``Chunk``
objects, so this is the one seam wide enough to carry it from chunk time
to embed time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from ragmonk.core.models import Document, DocumentFormat, Paragraph, Section, SectionKind, Table
from ragmonk.documents import table_renderer
from ragmonk.storage.repositories import links_repo

# Search Quality Improvement Plan, Phase 7: FTS5's ``bm25(table, w1, w2,
# ...)`` takes one weight per column *in the table's ``CREATE VIRTUAL
# TABLE`` declaration order* -- not by column name, and not skipping
# ``UNINDEXED`` columns, which still occupy a positional slot even though
# their weight value is ignored (they can never match, so they contribute
# nothing regardless). ``document_fts`` is declared as
# ``(section_id UNINDEXED, document_id UNINDEXED, heading_text, body,
# doc_title)`` (see ``storage/schema.py``'s ``KNOWLEDGE_DB_V3``), so this
# tuple's five positions are, in order: a placeholder for ``section_id``,
# a placeholder for ``document_id``, then the real weights for
# ``heading_text``, ``body``, ``doc_title``. Reordering this tuple to
# match the *intuitive* title/heading/body reading order instead of the
# schema's actual declaration order would silently misweight the wrong
# column -- SQLite raises no error, it just scores on the wrong field.
#
# Values are the search-quality plan's benchmark-driven starting point:
# a title or heading match is a strong, deliberate signal (a document is
# usually *about* what its title says) and should outrank many incidental
# body occurrences of the same term, without making body text worthless
# (``keyword_search``/multi-word queries still depend on it). Verified
# against ``benchmarks/search_quality`` before landing -- see
# ``CHANGELOG.md``'s Phase 7 entry for the before/after category numbers.
_DOCUMENT_FTS_COLUMN_WEIGHTS: tuple[float, float, float, float, float] = (
    0.0,  # section_id (UNINDEXED, ignored -- placeholder to keep position)
    0.0,  # document_id (UNINDEXED, ignored -- placeholder to keep position)
    5.0,  # heading_text
    1.0,  # body
    8.0,  # doc_title
)

# ``bm25(document_fts, ?, ?, ?, ?, ?)`` -- built once so both ``search_fts``
# and ``search_fts_projection`` below score identically rather than one of
# them drifting to FTS5's flat default weighting.
_BM25_DOCUMENT_FTS_EXPR = "bm25(document_fts, {}, {}, {}, {}, {})".format(
    *_DOCUMENT_FTS_COLUMN_WEIGHTS
)


@dataclass(frozen=True)
class DocumentUnit:
    """One ``document_sections`` row, kind-agnostic -- the shape
    ``knowledge/linker.py`` and ``knowledge/evidence.py`` actually need
    (heading/paragraph text, or -- since Phase 4 -- a table's own
    row-aware rendering, already sitting in ``text`` exactly as
    ``insert_table`` wrote it, so a linker match doesn't have to
    special-case table rows). Not a replacement for
    ``Section``/``Paragraph``/``Table``: those stay the Phase 3
    storage-facing shapes; this is a read-facing projection.
    """

    id: str
    document_id: str
    file_id: str
    kind: SectionKind
    text: str
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    # This row's stored embedding input (``chunker.Chunk.contextual_text``
    # at index time) -- "" for a row written before this field existed, or
    # by a caller that passed no ``embedding_text``, never ``None`` so
    # every reader (``embedding_indexer.py``) can treat it uniformly.
    embedding_text: str = ""


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
    caption: str | None,
    generation: int,
    created_at: str,
    fts_heading: str,
    fts_body: str,
    doc_title: str,
    embedding_text: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO document_sections (
            id, document_id, file_id, kind, heading_level, text,
            heading_path, parent_id, order_index, page_start, page_end,
            table_rows, num_rows, num_cols, caption, generation, created_at,
            embedding_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            caption,
            generation,
            created_at,
            embedding_text,
        ),
    )
    conn.execute(
        "INSERT INTO document_fts (section_id, document_id, heading_text, body, doc_title) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, document_id, fts_heading, fts_body, doc_title),
    )


def insert_section(
    conn: sqlite3.Connection,
    section: Section,
    *,
    doc_title: str,
    search_text: str | None = None,
    embedding_text: str | None = None,
) -> None:
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
        caption=None,
        generation=section.generation,
        created_at=section.created_at,
        fts_heading=section.text,
        fts_body=search_text if search_text is not None else "",
        doc_title=doc_title,
        embedding_text=embedding_text,
    )


def insert_paragraph(
    conn: sqlite3.Connection,
    paragraph: Paragraph,
    *,
    doc_title: str,
    search_text: str | None = None,
    embedding_text: str | None = None,
) -> None:
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
        caption=None,
        generation=paragraph.generation,
        created_at=paragraph.created_at,
        fts_heading=" > ".join(paragraph.heading_path),
        fts_body=search_text if search_text is not None else paragraph.text,
        doc_title=doc_title,
        embedding_text=embedding_text,
    )


def insert_table(
    conn: sqlite3.Connection,
    table: Table,
    *,
    doc_title: str,
    search_text: str | None = None,
    embedding_text: str | None = None,
) -> None:
    # Search Quality Improvement Plan, Phase 4: row-aware rendering
    # (``table_renderer.render_table``) replaces the previous flattened,
    # space-joined cell blob -- see that module's docstring for why
    # flattening loses row/column association ("which server has 85%
    # CPU?"). This is both the table's `text` (so it feeds
    # ``embed_touched_files`` via ``list_units_by_file`` -- the only
    # thing an embedding subject's text can be, table or not) and its
    # default FTS `body` (overridden by an explicit `search_text`, exactly
    # mirroring `insert_paragraph`'s own `search_text if search_text is not
    # None else paragraph.text` fallback). Heading-path/title context
    # still reaches FTS the same way it always has -- the
    # `fts_heading`/`doc_title` columns below -- rather than being
    # duplicated in here; a table-only special case for that would both
    # diverge from every other kind's own `text`/`fts_body` (always
    # exactly its own content, no prefix) and risk cross-domain-linker
    # false positives (`knowledge/linker.py` substring-matches entity
    # names against this same `text`).
    rendered = table_renderer.render_table(table.rows, caption=table.caption)
    _insert_row(
        conn,
        row_id=table.id,
        document_id=table.document_id,
        file_id=table.file_id,
        kind=SectionKind.TABLE,
        heading_level=None,
        text=rendered,
        heading_path=table.heading_path,
        parent_id=table.parent_id,
        order_index=table.order_index,
        page_start=table.page_start,
        page_end=table.page_end,
        table_rows=table.rows,
        num_rows=table.num_rows,
        num_cols=table.num_cols,
        caption=table.caption,
        generation=table.generation,
        created_at=table.created_at,
        fts_heading=" > ".join(table.heading_path),
        fts_body=search_text if search_text is not None else rendered,
        doc_title=doc_title,
        embedding_text=embedding_text,
    )


def _row_to_unit(row: sqlite3.Row) -> DocumentUnit:
    # A table's `text` is, since Phase 4, already its own row-aware
    # rendering (``insert_table``) -- no more re-deriving a flattened
    # blob from `table_rows` here, so every kind now reads back exactly
    # the text it was written with.
    return DocumentUnit(
        id=row["id"],
        document_id=row["document_id"],
        file_id=row["file_id"],
        kind=SectionKind(row["kind"]),
        text=row["text"] or "",
        heading_path=json.loads(row["heading_path"]) if row["heading_path"] else [],
        page_start=row["page_start"],
        page_end=row["page_end"],
        embedding_text=row["embedding_text"] or "",
    )


def get_unit(conn: sqlite3.Connection, unit_id: str) -> DocumentUnit | None:
    row = conn.execute("SELECT * FROM document_sections WHERE id = ?", (unit_id,)).fetchone()
    return _row_to_unit(row) if row is not None else None


@dataclass(slots=True, frozen=True)
class ChunkNeighbors:
    """Search Quality Improvement Plan, Phase 9: the units immediately
    surrounding a matched chunk -- its nearest enclosing heading and its
    previous/next siblings under that same heading, each nearest-first
    (``previous[-1]``/``next[0]`` are the immediately adjacent chunks).
    A missing piece (no parent, first/last sibling) comes back as
    ``None``/``[]`` rather than an error -- see ``get_chunk_neighbors``.
    """

    parent_heading: DocumentUnit | None
    previous: list[DocumentUnit]
    next: list[DocumentUnit]


def get_chunk_neighbors(
    conn: sqlite3.Connection,
    unit_id: str,
    *,
    previous_chunks: int = 0,
    next_chunks: int = 0,
    include_parent_heading: bool = True,
) -> ChunkNeighbors:
    """Context-expansion lookup for ``retrieval/context_builder.py``
    (blueprint Phase 9): reuses ``parent_id``/``order_index`` exactly as
    stored by ``documents/pipeline.py`` rather than adding any new
    column -- a chunk's siblings are every ``document_sections`` row
    sharing its ``parent_id`` (``IS`` handles the top-level ``NULL``
    case, where ``=`` would silently match nothing), and its nearest
    heading is simply the row its own ``parent_id`` points at (every
    ``parent_id`` a chunk carries is a ``HEADING`` row's id -- see
    ``documents/chunker.py``'s ``parent_index`` docstring).

    An unknown ``unit_id`` (already deleted, or from a different
    project's DB) degrades to the same all-empty result as a chunk with
    no neighbors, since a context-expansion lookup is never the thing
    that should turn an otherwise-successful search into an error.
    """
    self_row = conn.execute(
        "SELECT parent_id, order_index FROM document_sections WHERE id = ?", (unit_id,)
    ).fetchone()
    if self_row is None:
        return ChunkNeighbors(parent_heading=None, previous=[], next=[])

    parent_id = self_row["parent_id"]
    order_index = self_row["order_index"]

    parent_heading = (
        get_unit(conn, parent_id)
        if include_parent_heading and parent_id is not None
        else None
    )

    previous: list[DocumentUnit] = []
    if previous_chunks > 0:
        rows = conn.execute(
            "SELECT * FROM document_sections WHERE parent_id IS ? AND order_index < ? "
            "ORDER BY order_index DESC LIMIT ?",
            (parent_id, order_index, previous_chunks),
        ).fetchall()
        previous = [_row_to_unit(row) for row in reversed(rows)]

    next_units: list[DocumentUnit] = []
    if next_chunks > 0:
        rows = conn.execute(
            "SELECT * FROM document_sections WHERE parent_id IS ? AND order_index > ? "
            "ORDER BY order_index ASC LIMIT ?",
            (parent_id, order_index, next_chunks),
        ).fetchall()
        next_units = [_row_to_unit(row) for row in rows]

    return ChunkNeighbors(parent_heading=parent_heading, previous=previous, next=next_units)


def list_units_by_file(conn: sqlite3.Connection, file_id: str) -> list[DocumentUnit]:
    rows = conn.execute(
        "SELECT * FROM document_sections WHERE file_id = ? ORDER BY order_index", (file_id,)
    ).fetchall()
    return [_row_to_unit(row) for row in rows]


def list_units_by_files(conn: sqlite3.Connection, file_ids: Sequence[str]) -> list[DocumentUnit]:
    """Every unit belonging to any of ``file_ids``, in one query --
    indexing optimization plan V2, Phase P4: mirrors
    ``entities_repo.list_by_files``'s identical fix for the same caller
    (``indexing/embedding_indexer.prepare_embeddings``, across every
    touched document file in one source pass), which previously called
    ``list_units_by_file`` once per file -- N round trips for N touched
    files, measured at 300 statements for a 300-file batch, down to 1.
    Ordered by ``file_id, order_index`` so a caller grouping results back
    out per file gets each file's own units in their original chunk
    order.
    """
    if not file_ids:
        return []
    placeholders = ", ".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT * FROM document_sections WHERE file_id IN ({placeholders}) "
        "ORDER BY file_id, order_index",
        tuple(file_ids),
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


def iter_embedding_texts(conn: sqlite3.Connection) -> Iterator[str]:
    """Yields every stored section's ``embedding_text`` (a chunk's exact
    contextual payload). Backs the Exact Tokenizer plan's ``doctor``
    payload-invariant scan (Phase 5) -- streamed rather than materialized
    so a large index doesn't build one giant list.
    """
    cursor = conn.execute(
        "SELECT embedding_text FROM document_sections "
        "WHERE embedding_text IS NOT NULL AND embedding_text != ''"
    )
    for row in cursor:
        yield str(row["embedding_text"])


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
        f"""
        SELECT document_id, section_id, heading_text, body, doc_title
        FROM document_fts
        WHERE document_fts MATCH ?
        ORDER BY {_BM25_DOCUMENT_FTS_EXPR}
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
    # Search Quality Improvement Plan, Phase 8: the raw ``bm25()`` value
    # FTS5 already computes for ``ORDER BY`` (lower/more negative is a
    # better match) -- ``fts_rank`` above only ever kept this query's
    # relative *position*, discarding the magnitude; ``retrieval/
    # merger.py`` now carries this through onto ``SearchCandidate.
    # bm25_score`` so it survives hybrid fusion as its own signal instead
    # of being silently dropped. ``None`` for a row this query plan never
    # produced (e.g. an exact-title hit with no FTS row at all).
    bm25_score: float | None = None
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
        f"""
        SELECT df.document_id, df.section_id, df.heading_text, df.body,
               df.doc_title, d.title AS document_title, f.path, f.mtime,
               ds.page_start, ds.page_end, ds.heading_path,
               {_BM25_DOCUMENT_FTS_EXPR} AS rank,
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
                bm25_score=row["rank"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                heading_path=json.loads(row["heading_path"]) if row["heading_path"] else [],
            )
        )
    return results
