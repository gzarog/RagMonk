"""DDL for the two SQLite databases RAGpilot maintains.

``sources.db`` is the single global registry of sources. Each registered
source gets its own ``knowledge.db`` ("project") holding the files it has
discovered plus that project's job queue and error log.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = 2

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

_METADATA_TABLE = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

SOURCES_DB_V1: tuple[str, ...] = (
    _MIGRATIONS_TABLE,
    _METADATA_TABLE,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        indexing_mode TEXT NOT NULL DEFAULT 'full',
        include_patterns TEXT NOT NULL DEFAULT '[]',
        exclude_patterns TEXT NOT NULL DEFAULT '[]',
        last_scan_at TEXT,
        last_error TEXT,
        fingerprint TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

# Phase 7: online/offline source status (blueprint's incremental runtime --
# see ``core.models.SourceStatus``). Additive-only migration layered on top
# of SOURCES_DB_V1 -- see storage/migrations. A plain ``ALTER TABLE ADD
# COLUMN`` with a default keeps every already-registered source ``active``
# without a backfill statement.
SOURCES_DB_V2: tuple[str, ...] = (
    "ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
)

KNOWLEDGE_DB_V1: tuple[str, ...] = (
    _MIGRATIONS_TABLE,
    _METADATA_TABLE,
    """
    CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        path TEXT NOT NULL,
        kind TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        content_hash TEXT,
        status TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        parser_version TEXT NOT NULL DEFAULT '1',
        last_indexed_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(source_id, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_jobs (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        file_id TEXT NOT NULL REFERENCES files(id),
        job_type TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        error_code TEXT,
        error_message TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_index_jobs_status ON index_jobs(status)",
    """
    CREATE TABLE IF NOT EXISTS index_errors (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        file_id TEXT,
        path TEXT,
        error_code TEXT NOT NULL,
        error_message TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )
    """,
)

# Phase 2: code entities/relationships extracted by Tree-sitter, plus an
# FTS5 index over their names for lexical lookup. Additive-only migration
# layered on top of KNOWLEDGE_DB_V1 -- see storage/migrations.
KNOWLEDGE_DB_V2: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        file_id TEXT NOT NULL REFERENCES files(id),
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        qualified_name TEXT NOT NULL,
        language TEXT NOT NULL,
        parent_id TEXT REFERENCES entities(id),
        signature TEXT,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        start_col INTEGER NOT NULL DEFAULT 0,
        end_col INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(file_id, qualified_name, start_line)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_file ON entities(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)",
    "CREATE INDEX IF NOT EXISTS idx_entities_qualified_name ON entities(qualified_name)",
    "CREATE INDEX IF NOT EXISTS idx_entities_parent ON entities(parent_id)",
    """
    CREATE TABLE IF NOT EXISTS relationships (
        id TEXT PRIMARY KEY,
        relationship_type TEXT NOT NULL,
        source_entity_id TEXT NOT NULL REFERENCES entities(id),
        target_entity_id TEXT REFERENCES entities(id),
        target_symbol TEXT,
        resolver TEXT NOT NULL,
        confidence TEXT NOT NULL,
        file_id TEXT NOT NULL REFERENCES files(id),
        source_location TEXT,
        evidence TEXT,
        generation INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relationships_file ON relationships(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_relationships_target_symbol ON relationships(target_symbol)",
    "CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(relationship_type)",
    # Not an external-content FTS table: rows are written explicitly by the
    # code processor alongside entities (not via SQL triggers) so the
    # "delete old generation, insert new, all in one transaction" atomicity
    # rule in coordinator.py stays a single, auditable code path.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
        entity_id UNINDEXED,
        name,
        qualified_name,
        snippet
    )
    """,
)

# Phase 3: Docling-derived document structure (headings/paragraphs/tables)
# plus an FTS5 index over their text, mirroring KNOWLEDGE_DB_V2's
# entities/relationships/code_fts shape. Additive-only migration layered on
# top of KNOWLEDGE_DB_V2 -- see storage/migrations.
KNOWLEDGE_DB_V3: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        file_id TEXT NOT NULL REFERENCES files(id),
        format TEXT NOT NULL,
        title TEXT,
        author TEXT,
        page_count INTEGER,
        section_count INTEGER NOT NULL DEFAULT 0,
        paragraph_count INTEGER NOT NULL DEFAULT 0,
        table_count INTEGER NOT NULL DEFAULT 0,
        is_scanned INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT,
        generation INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(file_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id)",
    # One table for all three unit kinds (heading/paragraph/table), a
    # ``kind`` discriminator, mirroring how KNOWLEDGE_DB_V2 keeps every
    # code entity kind in one ``entities`` table rather than one table per
    # ``EntityType``. ``table_rows`` is a JSON row-major grid, populated
    # only for kind='table'.
    """
    CREATE TABLE IF NOT EXISTS document_sections (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(id),
        file_id TEXT NOT NULL REFERENCES files(id),
        kind TEXT NOT NULL,
        heading_level INTEGER,
        text TEXT NOT NULL DEFAULT '',
        heading_path TEXT NOT NULL DEFAULT '[]',
        parent_id TEXT REFERENCES document_sections(id),
        order_index INTEGER NOT NULL,
        page_start INTEGER,
        page_end INTEGER,
        table_rows TEXT,
        num_rows INTEGER,
        num_cols INTEGER,
        generation INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_document_sections_document "
    "ON document_sections(document_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_document_sections_file ON document_sections(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_document_sections_parent ON document_sections(parent_id)",
    # Not an external-content FTS table, for the same reason as code_fts:
    # rows are written explicitly alongside document_sections (not via SQL
    # triggers) so the atomic "delete previous generation, insert new"
    # rule stays one auditable code path (see documents/pipeline.py).
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        section_id UNINDEXED,
        document_id UNINDEXED,
        heading_text,
        body,
        doc_title
    )
    """,
)

# Phase 4: cross-domain (code entity <-> document) links, plus explicit
# user-defined mappings. A parallel table rather than a reuse of
# KNOWLEDGE_DB_V2's ``relationships`` -- see ``core.models.CrossLink``'s
# docstring for why ``relationships``' entity-only foreign keys don't
# generalize to a document target. Additive-only migration layered on top
# of KNOWLEDGE_DB_V3 -- see storage/migrations.
KNOWLEDGE_DB_V4: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS cross_links (
        id TEXT PRIMARY KEY,
        link_type TEXT NOT NULL,
        entity_id TEXT NOT NULL REFERENCES entities(id),
        document_id TEXT NOT NULL REFERENCES documents(id),
        section_id TEXT REFERENCES document_sections(id),
        resolver TEXT NOT NULL,
        confidence TEXT NOT NULL,
        evidence TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(entity_id, document_id, section_id, resolver)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cross_links_entity ON cross_links(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_cross_links_document ON cross_links(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_cross_links_section ON cross_links(section_id)",
)

# Phase 9: local text-embedding vectors for real semantic search
# (blueprint section 53), stored separately from and additive to
# ``entities``/``documents``/``document_sections`` -- losing or clearing
# this table can never corrupt or even touch those authoritative rows, it
# only turns semantic search back into "unavailable" (see
# retrieval/semantic.py's graceful degradation). No foreign key to either
# source table: an embedding subject is one of two tables
# (``core.models.EmbeddingSubjectType``), and SQLite has no cross-table
# conditional FK -- ``file_id`` (present on both) is enough for the one
# thing this table's own writer needs, deleting a stale generation
# (``embeddings_repo.delete_by_file``, mirroring ``entities_repo``/
# ``documents_repo``'s own delete_by_file).
#
# ``model_id``/``dim`` are stamped on every row, not stored once for the
# whole table: if the configured embedding model ever changes,
# similarity search (``retrieval/vectorstore.py``) simply filters rows to
# the current ``model_id`` rather than ever mixing vectors from two
# models in one comparison -- see retrieval/embedder.py's docstring for
# why this is "exclude stale rows", not "auto re-embed the whole
# project" (mirrors knowledge/linker.py's own "only files touched this
# run" scoping tradeoff).
#
# Brute-force cosine similarity over these rows in application code
# (retrieval/vectorstore.py), not a loadable SQLite extension like
# sqlite-vec: Python's ``sqlite3`` module's ``enable_load_extension``
# support is not guaranteed available/enabled on every platform/Python
# build, exactly the kind of cross-platform risk this project has hit
# before (see CHANGELOG's Phase 5-7 Windows/macOS-specific fixes) and
# cannot verify here without a live multi-OS test -- a plain BLOB column
# plus a linear scan is a legitimate, blueprint-sanctioned "equivalent
# embedded index" at the scale a local per-project knowledge base
# actually operates at.
KNOWLEDGE_DB_V5: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS embeddings (
        id TEXT PRIMARY KEY,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vector BLOB NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(subject_type, subject_id, model_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_embeddings_file ON embeddings(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_id)",
)

# PDF documents' expensive step -- Docling's real layout/table-structure
# pipeline -- is cached keyed by the file's *content hash* rather than its
# file id or path, so a moved, renamed, or duplicated PDF with identical
# bytes still hits the cache, and re-indexing an unchanged PDF never
# re-runs the model.
#
# Originally (Phase 3) this cached the pipeline's Markdown *export*, which
# was then reparsed through Docling's separate Markdown backend into the
# document that actually got normalized/chunked/indexed -- see git history
# for that rationale. Phase 1B (search-quality improvement plan) replaced
# that round-trip with caching the native ``DoclingDocument`` itself (see
# ``documents/docling_adapter.py``'s module docstring); ``KNOWLEDGE_DB_V10``
# below reshapes this same table for that, in place, rather than
# introducing a new one.
#
# ``cache_version`` guards against a cached row being misinterpreted after
# ``docling_adapter``'s serialization scheme changes: a lookup that finds a
# row with a stale ``cache_version`` must treat it as a miss (see
# ``document_conversion_cache_repo.get``), not hand back a document that no
# longer matches how callers deserialize it.
#
# Purely derived, disposable state -- like ``embeddings``, losing this
# table only costs a slower next index run, never correctness -- and it is
# never actively pruned: a changed PDF gets a new ``content_hash`` and a
# new row, and the stale row for the old hash is simply orphaned. That is
# an acceptable, unbounded-but-slow-growing cost at this project's scale,
# matching ``embeddings``' own "never actively GC'd" precedent.
KNOWLEDGE_DB_V6: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS document_conversion_cache (
        content_hash TEXT PRIMARY KEY,
        markdown TEXT NOT NULL,
        page_count INTEGER,
        cache_version INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

# Search performance redesign, phase 1 (blueprint sections 7/10): an
# indexed ``alias`` column so ``retrieval/lexical.py``'s "Class.member"
# alias lookup (see ``entities_repo.compute_alias``) is a ``WHERE alias =
# ?`` index seek instead of a full-corpus Python scan over every entity,
# plus a case-insensitive index backing exact document-title lookup the
# same way. Both are additive; ``schema_migrations`` records this
# migration as ``5, 6`` are already recorded, so it only ever runs once
# per database, and any entity row inserted before this migration simply
# has ``alias IS NULL`` until its file is next reindexed (``entities_repo.
# insert`` populates it going forward) -- a stale-until-reindexed gap
# already accepted by this project's own generational delete-then-insert
# model, not a new one.
KNOWLEDGE_DB_V7: tuple[str, ...] = (
    "ALTER TABLE entities ADD COLUMN alias TEXT",
    # Backfills every already-indexed entity's alias in one statement
    # (rather than leaving it ``NULL`` until its file is next reindexed):
    # a recursive CTE splits ``qualified_name`` on '.', then joins its
    # last two segments -- the exact rule ``entities_repo.compute_alias``
    # applies to every entity inserted from here on. Uses two correlated
    # scalar subqueries (picking segment ``n = total-1`` and ``n = total``
    # by position) rather than ``group_concat`` over an ``ORDER BY``
    # subquery: SQLite does not guarantee ``group_concat`` consumes rows
    # in a subquery's ordered sequence without an enclosing ``GROUP BY``,
    # which silently produced the two segments in the wrong order when
    # tried.
    """
    UPDATE entities
    SET alias = (
        WITH RECURSIVE segs(rest, seg, n) AS (
            SELECT qualified_name || '.', '', 0
            UNION ALL
            SELECT substr(rest, instr(rest, '.') + 1),
                   substr(rest, 1, instr(rest, '.') - 1),
                   n + 1
            FROM segs
            WHERE rest <> ''
        ),
        total(n) AS (SELECT MAX(n) FROM segs)
        SELECT (SELECT seg FROM segs WHERE n = total.n - 1)
               || '.' || (SELECT seg FROM segs WHERE n = total.n)
        FROM total
        WHERE total.n >= 3
    )
    WHERE alias IS NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_alias ON entities(alias)",
    "CREATE INDEX IF NOT EXISTS idx_documents_title_nocase ON documents(title COLLATE NOCASE)",
)

# Search performance redesign, phase 2 (blueprint section 11): a
# dedicated FTS5 index over file paths so ``ragpilot search``'s path
# matching is an indexed lookup instead of ``files_repo.
# search_by_substring``'s ``LIKE '%query%'`` sequential scan. Populated
# alongside ``files`` writes (``files_repo.insert``/``delete``). No
# separate tokenized column: FTS5's default ``unicode61`` tokenizer
# already splits on ``/``, ``.``, and other non-alphanumeric characters
# when it indexes ``path``, so "vectorstore" and "py" are already
# independently searchable tokens of "src/ragpilot/retrieval/
# vectorstore.py" without any pre-tokenization. Plain substring ``LIKE``
# is kept as a fallback for fragments that don't align to a token
# boundary (e.g. "ectorstore" mid-word) -- see
# ``files_repo.search_path_projection``.
KNOWLEDGE_DB_V8: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS path_fts USING fts5(
        file_id UNINDEXED,
        path
    )
    """,
    "INSERT INTO path_fts (file_id, path) SELECT id, path FROM files",
)

# Search performance redesign, phase 3 (blueprint section 13): the
# integer-key <-> subject mapping a persistent ANN index needs, since
# ``usearch``/``hnswlib`` keys are compact integers, not this project's
# UUID-shaped ``entities.id``/``document_sections.id``. Additive and, like
# ``embeddings`` (``KNOWLEDGE_DB_V5``), entirely disposable: dropping every
# row here only costs a lazy ANN rebuild (``retrieval/ann.py``), never
# touches ``entities``/``documents``. ``vector_id`` is
# ``INTEGER PRIMARY KEY AUTOINCREMENT`` specifically so a deleted row's id
# is never reused by a later insert -- an ANN index that still has a
# stale entry under a reused id would silently resolve to the wrong
# subject. ``file_id`` (present on ``embeddings`` for the same reason) is
# what lets ``retrieval/ann.py`` delete a touched file's previous
# generation of vector ids before re-adding its current ones, mirroring
# ``embeddings_repo.delete_by_file``.
KNOWLEDGE_DB_V9: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS vector_items (
        vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        UNIQUE(subject_type, subject_id, model_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vector_items_subject ON vector_items(subject_type, subject_id)",
    "CREATE INDEX IF NOT EXISTS idx_vector_items_model ON vector_items(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_vector_items_file ON vector_items(file_id)",
)

# Search-quality improvement plan, Phase 1B: ``document_conversion_cache``
# (``KNOWLEDGE_DB_V6``) switches from caching a PDF's Markdown *export* to
# caching the native ``DoclingDocument`` Docling's real PDF pipeline
# produces, serialized whole (see ``documents/docling_adapter.py``'s module
# docstring for the full before/after). The row shape barely changes --
# ``markdown`` held one flavor of serialized text, ``serialized_document``
# holds another -- so this reshapes the existing table in place rather
# than creating a new one:
#
#   * ``RENAME COLUMN`` repurposes ``markdown`` as ``serialized_document``
#     without touching any row's bytes -- a genuinely additive, purely
#     catalog-level change. Every row written before this migration keeps
#     its old Markdown text sitting under the new name, inert: the
#     ``_CACHE_VERSION`` bump in ``docling_adapter.py`` (accompanying this
#     migration) means such a row's ``cache_version`` no longer matches
#     what ``document_conversion_cache_repo.get`` looks up, so it is
#     always treated as a miss and never actually read back as if it held
#     a serialized ``DoclingDocument``. This is this project's documented
#     migration strategy for this table: don't convert old cache rows,
#     bump the version so they are treated as stale and get regenerated
#     natively next time their PDF is indexed.
#   * ``serialization_format``/``parser_version`` are new, nullable
#     columns (``ADD COLUMN``, no backfill -- exactly ``KNOWLEDGE_DB_V7``'s
#     precedent for a column only rows written going forward populate).
#
# ``page_count`` and ``cache_version``/``created_at`` are unchanged.
KNOWLEDGE_DB_V10: tuple[str, ...] = (
    "ALTER TABLE document_conversion_cache RENAME COLUMN markdown TO serialized_document",
    "ALTER TABLE document_conversion_cache ADD COLUMN serialization_format TEXT",
    "ALTER TABLE document_conversion_cache ADD COLUMN parser_version TEXT",
)

# Search-quality improvement plan, Phase 3: ``document_sections`` gains
# ``embedding_text`` -- the document-title + heading-path-contextualized
# rendering of a row's ``text`` that ``documents/chunker.py`` computes at
# chunk time (``Chunk.contextual_text``) and ``documents/pipeline.py`` now
# stores verbatim. Nullable, no backfill, exactly ``KNOWLEDGE_DB_V7``'s
# precedent: a row written before this migration simply has
# ``embedding_text IS NULL`` until its file is next reindexed, and
# ``documents_repo.DocumentUnit``/``indexing/embedding_indexer.py`` both
# treat that the same as "" -- fall back to embedding the row's own
# ``text`` rather than erroring or leaving the row unembedded.
#
# Kept as its own persisted column rather than only ever recomputed at
# embed time: ``embedding_indexer.py`` embeds already-stored
# ``document_sections`` rows (not live ``Chunk`` objects), and having the
# exact contextualized string actually embedded sitting on the row itself
# makes what got embedded inspectable/debuggable without re-deriving it
# from ``documents``/``document_sections`` by hand.
#
# The FTS-lexical counterpart (``Chunk.search_text``) is deliberately NOT
# given its own column here: it is written straight into
# ``document_fts``'s existing indexed ``body`` column
# (``documents_repo.insert_section``/``insert_paragraph``/``insert_table``),
# which is itself already the "store lexical FTS fields explicitly" the
# search-quality plan asks for -- a second, redundant copy on
# ``document_sections`` would have no reader.
KNOWLEDGE_DB_V11: tuple[str, ...] = (
    "ALTER TABLE document_sections ADD COLUMN embedding_text TEXT",
)

# Search Quality Improvement Plan, Phase 5: ``document_conversion_cache``
# (``KNOWLEDGE_DB_V6``/``V10``) gains ``ocr_used`` -- ``"off"`` or ``"on"``,
# whichever pipeline (plain vs. OCR-enabled) actually produced that row's
# ``serialized_document`` -- and it joins ``content_hash`` as this table's
# key, replacing ``content_hash`` alone as the primary key. See
# ``documents/docling_adapter.py``'s module docstring and
# ``document_conversion_cache_repo``'s for the full rationale: a PDF's
# plain and OCR'd conversions are genuinely different documents, and a
# cache lookup that couldn't distinguish them could hand either one back
# for a request that needed the other.
#
# SQLite has no ``ALTER TABLE ... DROP CONSTRAINT``/"change primary key"
# statement, so this rebuilds the table under a temporary name rather than
# altering it in place -- the same "rename, recreate, copy forward, drop"
# shape a primary-key change always needs in SQLite. Unlike
# ``KNOWLEDGE_DB_V10``'s deliberate "bump cache_version, let every old row
# go stale" choice (there was no way to know *which* pipeline had produced
# an old Markdown-keyed row), every row that exists before this migration
# was, unambiguously, produced by the plain pipeline -- OCR did not exist
# yet -- so this backfills ``ocr_used = 'off'`` for all of them rather
# than discarding a cache that is still entirely valid.
KNOWLEDGE_DB_V13: tuple[str, ...] = (
    "ALTER TABLE document_conversion_cache RENAME TO document_conversion_cache_pre_ocr",
    """
    CREATE TABLE document_conversion_cache (
        content_hash TEXT NOT NULL,
        ocr_used TEXT NOT NULL DEFAULT 'off',
        serialized_document TEXT NOT NULL,
        serialization_format TEXT,
        page_count INTEGER,
        parser_version TEXT,
        cache_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (content_hash, ocr_used)
    )
    """,
    """
    INSERT INTO document_conversion_cache (
        content_hash, ocr_used, serialized_document, serialization_format,
        page_count, parser_version, cache_version, created_at
    )
    SELECT content_hash, 'off', serialized_document, serialization_format,
        page_count, parser_version, cache_version, created_at
    FROM document_conversion_cache_pre_ocr
    """,
    "DROP TABLE document_conversion_cache_pre_ocr",
)
