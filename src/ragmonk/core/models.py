"""Shared, schema-level data models used across the indexing pipeline.

These are plain Pydantic models (not an ORM) that mirror the SQLite row
shapes defined in ``storage.schema``. Repositories translate between rows
and these models.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SourceType(StrEnum):
    LOCAL = "local"
    NETWORK = "network"


class SourceStatus(StrEnum):
    """Whether a source's root was reachable on its most recent scan
    attempt (``ragmonk index`` or the Phase 7 daemon) -- distinct from
    whether the source is *enabled*. ``OFFLINE`` means the root itself
    (not an individual file inside it) could not be listed: an unmounted
    network share, a revoked permission, a deleted directory. See
    ``sources/scanner.py``'s ``check_root_accessible`` and
    ``indexing/runner.py`` for the mechanism that sets this.
    """

    ACTIVE = "active"
    OFFLINE = "offline"


class IndexingMode(StrEnum):
    FULL = "full"


class FileKind(StrEnum):
    CODE = "code"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class FileStatus(StrEnum):
    DISCOVERED = "discovered"
    CLASSIFIED = "classified"
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY = "retry"
    FAILED = "failed"
    INDEXED = "indexed"
    SKIPPED_LIMIT = "skipped_limit"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY = "retry"
    FAILED = "failed"
    COMPLETED = "completed"


class JobType(StrEnum):
    INDEX_FILE = "index_file"


class Source(BaseModel):
    model_config = ConfigDict(frozen=False)

    id: str
    path: str
    source_type: SourceType
    enabled: bool = True
    indexing_mode: IndexingMode = IndexingMode.FULL
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []
    last_scan_at: str | None = None
    last_error: str | None = None
    fingerprint: str | None = None
    status: SourceStatus = SourceStatus.ACTIVE
    created_at: str
    updated_at: str


class FileRecord(BaseModel):
    id: str
    source_id: str
    path: str
    kind: FileKind
    size: int
    mtime: float
    content_hash: str | None = None
    status: FileStatus
    generation: int = 0
    parser_version: str = "1"
    # Search Quality Improvement Plan, Phase 12: the rest of this file's
    # composite reuse identity, alongside `content_hash`/`parser_version`
    # above -- see `storage/schema.py`'s `KNOWLEDGE_DB_V14` for why all
    # three are nullable with no backfill, and `indexing/incremental.
    # decide_reprocessing` for how they gate reprocessing.
    chunker_version: str | None = None
    embedding_model_id: str | None = None
    embedding_text_version: str | None = None
    last_indexed_at: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class IndexJob(BaseModel):
    id: str
    source_id: str
    file_id: str
    job_type: JobType
    status: JobStatus
    priority: int = 0
    attempt_count: int = 0
    next_attempt_at: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class IndexErrorRecord(BaseModel):
    id: str
    source_id: str
    file_id: str | None = None
    path: str | None = None
    error_code: str
    error_message: str
    occurred_at: str


class ScannedFile(BaseModel):
    """A file found on disk during a scan, before it has a DB identity."""

    path: str
    size: int
    mtime: float


class EntityType(StrEnum):
    """Code entity kinds populated by Phase 2 extraction.

    ``Repository`` and ``Project`` from the blueprint's entity menu are
    deliberately not included: Phase 1's source/file tables already anchor
    project scope (one source == one ``knowledge.db``), so a redundant
    entity for it would carry no information yet. They can be added once a
    later phase needs multi-project-per-repository structure.
    """

    NAMESPACE = "namespace"
    CLASS = "class"
    INTERFACE = "interface"
    STRUCT = "struct"
    ENUM = "enum"
    FUNCTION = "function"
    METHOD = "method"
    PROPERTY = "property"
    FIELD = "field"


class RelationshipType(StrEnum):
    CALLS = "calls"
    IMPLEMENTS = "implements"
    EXTENDS = "extends"
    IMPORTS = "imports"
    REFERENCES = "references"
    CONTAINS = "contains"
    DEFINED_IN = "defined_in"
    # Phase 4 cross-domain types (code entity <-> document/section). Only
    # these three of the blueprint's catalog are populated -- PRODUCES/
    # CONSUMES/DEPENDS_ON/REQUIRES/TESTED_BY/AFFECTS/SUPERSEDES have no
    # real signal in what Phases 1-3 extract, so they are left out rather
    # than added unused. See knowledge/linker.py for which resolver
    # assigns which of these.
    DOCUMENTED_BY = "documented_by"
    MENTIONED_IN = "mentioned_in"
    RELATED_TO = "related_to"


class Confidence(StrEnum):
    """Confidence tiers for a resolved relationship.

    Ordered roughly EXACT > HIGH > MEDIUM > HEURISTIC, but the axis is not
    purely "how sure are we" -- HEURISTIC specifically marks a relationship
    inferred by a framework-pattern heuristic (``framework_rules.py``)
    rather than a generic language fact, and is never assigned to a plain
    AST-derived edge even an unresolved one. See ``code/resolver.py`` for
    the exact rule each tier maps to.
    """

    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    HEURISTIC = "heuristic"


class Entity(BaseModel):
    id: str
    source_id: str
    file_id: str
    kind: EntityType
    name: str
    qualified_name: str
    language: str
    parent_id: str | None = None
    signature: str | None = None
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0
    generation: int
    created_at: str
    updated_at: str


class Relationship(BaseModel):
    id: str
    relationship_type: RelationshipType
    source_entity_id: str
    target_entity_id: str | None = None
    target_symbol: str | None = None
    resolver: str
    confidence: Confidence
    file_id: str
    source_location: str | None = None
    evidence: str | None = None
    generation: int
    created_at: str


class CrossLink(BaseModel):
    """A Phase 4 cross-domain fact linking one code ``Entity`` to one
    ``Document`` (optionally pinned to a specific ``Section``/
    ``Paragraph``/``Table`` row within it).

    Kept as a table distinct from ``relationships`` rather than reusing it
    directly: ``relationships.source_entity_id``/``target_entity_id`` are
    both ``FOREIGN KEY REFERENCES entities(id)``, and a document is not an
    entity row, so writing a document id into ``target_entity_id`` would
    violate that constraint under ``PRAGMA foreign_keys = ON`` rather than
    generalize cleanly -- see storage/schema.py's ``KNOWLEDGE_DB_V4``.
    """

    id: str
    link_type: RelationshipType
    entity_id: str
    document_id: str
    section_id: str | None = None
    resolver: str
    confidence: Confidence
    evidence: str | None = None
    created_at: str


class DocumentFormat(StrEnum):
    """Every document format ``docling_adapter.convert`` genuinely
    converts. Still narrower than ``sources.detector.DOCUMENT_EXTENSIONS``
    -- legacy binary Office formats (``.doc``/``.ppt``/``.xls``) and RTF
    are classified as ``FileKind.DOCUMENT`` for routing but are not
    converted, because Docling's own backends for them require a working
    LibreOffice (``soffice``) subprocess with no pure-Python fallback --
    a system dependency this project deliberately does not add (see
    ``documents/docling_adapter.py``'s module docstring, "Search Quality
    Improvement Plan, Phase 10"). Outlook ``.msg`` is deferred for a
    different reason -- see that same docstring.

    Phase 10 additions: ``CSV``/``ODT``/``ODS``/``ODP``/``EPUB`` are
    genuinely converted by Docling's own rule-based (non-LibreOffice)
    backends -- ``ODT``/``ODS``/``ODP`` need the ``odfdo`` library, this
    phase's one new dependency (see ``pyproject.toml``'s comment; it is
    pure Python and needs nothing beyond the already-required ``lxml``).
    ``IMAGE`` (``.png``/``.jpg``/``.jpeg``/``.tif``/``.tiff``) is
    genuinely convertible too, but only *with* OCR -- an image has no
    embedded text layer to fall back to the way a PDF does -- so it is
    gated behind ``documents.image_ocr`` (default off, since every image
    then pays for a real OCR pass) rather than being unconditionally
    supported like the others.
    """

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    MARKDOWN = "markdown"
    TXT = "txt"
    EML = "eml"
    CSV = "csv"
    ODT = "odt"
    ODS = "ods"
    ODP = "odp"
    EPUB = "epub"
    IMAGE = "image"


class SectionKind(StrEnum):
    """Discriminator for a row in ``document_sections``: a heading node in
    the document's outline, a paragraph-level text chunk, or a table.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"


class EmbeddingSubjectType(StrEnum):
    """Discriminator for a row in ``embeddings`` (Phase 9): which
    already-stored, authoritative row a vector was computed from -- a
    code ``entities`` row or a ``document_sections`` row. Mirrors
    ``SectionKind``'s role for ``document_sections`` itself; kept as its
    own enum rather than reusing ``EntityType``/``SectionKind`` since an
    embedding subject is one of exactly two *kinds of table*, not one of
    either table's own finer-grained kind column.
    """

    ENTITY = "entity"
    DOCUMENT_SECTION = "document_section"


class Document(BaseModel):
    """One row per successfully converted document file, mirroring how
    ``FileRecord`` anchors a code file -- ``Entity``/``Section`` rows are
    this document's children the same way code entities are a file's.
    """

    id: str
    source_id: str
    file_id: str
    format: DocumentFormat
    title: str | None = None
    author: str | None = None
    page_count: int | None = None
    section_count: int = 0
    paragraph_count: int = 0
    table_count: int = 0
    is_scanned: bool = False
    content_hash: str | None = None
    generation: int
    created_at: str
    updated_at: str


class Section(BaseModel):
    """A heading node in a document's outline (``kind == HEADING``).

    ``heading_path`` is the titles of this heading's ancestors, root
    first, not including this heading's own title -- kept redundantly on
    every row (rather than requiring a walk up ``parent_id``) so a single
    row carries its own provenance for display without extra queries,
    matching the blueprint's "never return knowledge without traceable
    evidence" rule.
    """

    id: str
    document_id: str
    file_id: str
    kind: SectionKind = SectionKind.HEADING
    heading_level: int
    text: str
    heading_path: list[str] = []
    parent_id: str | None = None
    order_index: int
    page_start: int | None = None
    page_end: int | None = None
    generation: int
    created_at: str


class Paragraph(BaseModel):
    """A paragraph-level (or size-bounded, chunked) body text unit
    (``kind == PARAGRAPH``), stored in the same ``document_sections``
    table as ``Section``/``Table`` rows -- see ``storage/schema.py``.
    """

    id: str
    document_id: str
    file_id: str
    kind: SectionKind = SectionKind.PARAGRAPH
    text: str
    heading_path: list[str] = []
    parent_id: str | None = None
    order_index: int
    page_start: int | None = None
    page_end: int | None = None
    generation: int
    created_at: str


class Table(BaseModel):
    """A table (``kind == TABLE``), its cells stored as a row-major grid
    of strings rather than individual cell rows -- there is no query need
    yet for addressing a single cell, and this keeps one table one row.

    A large source table split by ``documents/chunker.py`` (Search
    Quality Improvement Plan, Phase 4) into several row-boundary chunks
    becomes several ``Table`` rows, each self-contained: its own ``rows``
    already has that table's header rows repeated at the top, so nothing
    downstream needs to know which ``Table`` row is a "continuation" of
    another.
    """

    id: str
    document_id: str
    file_id: str
    kind: SectionKind = SectionKind.TABLE
    heading_path: list[str] = []
    parent_id: str | None = None
    rows: list[list[str]]
    num_rows: int
    num_cols: int
    order_index: int
    page_start: int | None = None
    page_end: int | None = None
    # This table's caption text (``NormalizedUnit.caption`` /
    # ``Chunk.caption``, Phase 2), persisted from here on (Phase 4) --
    # previously computed but never actually stored anywhere, silently
    # dropped between chunking and storage. See ``documents_repo.
    # insert_table`` for how it is both kept as its own column (mirroring
    # `heading_path`'s raw-JSON-column treatment) and folded into the
    # table's searchable/embeddable text.
    caption: str | None = None
    generation: int
    created_at: str
