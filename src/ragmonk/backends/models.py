"""Backend-neutral data models for the ``KnowledgeBackend`` contract.

Storage backend abstraction plan, Phase 3: these dataclasses now carry the
actual payloads the local SQLite write path needs -- fleshed out from the
minimal Phase 1 stubs by reading the current ``code/processor.py``,
``documents/pipeline.py``, ``indexing/embedding_indexer.py`` and
``knowledge/linker.py`` persistence code and naming exactly the fields
those call sites use. Domain types reused here (``Entity``,
``Relationship``, ``Document``, ``Chunk``, ``LinkCandidate``) are already
backend-neutral -- plain dataclasses/pydantic models with no SQLite import
-- so importing them here does not pull SQLite (or any server SDK) into
this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ragmonk.core.models import (
    Confidence,
    Document,
    EmbeddingSubjectType,
    Entity,
    Relationship,
    RelationshipType,
)
from ragmonk.documents.chunker import Chunk


@dataclass(slots=True)
class FileRecord:
    """A single indexed file's identity and metadata, independent of any
    backend's storage shape.
    """

    file_id: str
    source_id: str
    path: str
    content_hash: str
    size_bytes: int = 0
    mtime: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Completion plan F6: the write generation this file record belongs
    # to on a server backend (``None`` = the source's currently published
    # generation). File metadata visibility tracks publication exactly
    # like entities/chunks do, so a failed rebuild never exposes a
    # half-written file listing.
    generation: int | None = None


@dataclass(slots=True)
class PreparedCode:
    """The write-half payload for one code file's entities/relationships
    -- everything ``code.processor.publish_code`` used to hand straight
    to ``entities_repo``/``relationships_repo``, now named here instead.

    ``entities``/``relationships`` are fully resolved and id-assigned
    already (cross-file symbol resolution reads the live project
    database and stays in ``code.processor.publish_code``, which is a
    *query*, not a write -- see that module's docstring); this dataclass
    only carries what still needs to be written. ``entity_snippets`` maps
    an entity's id to the FTS snippet text ``entities_repo.insert`` takes
    alongside it. ``clear_only`` is true for a recognized code extension
    with no Tree-sitter grammar yet (``language is None`` in
    ``code.processor.prepare_code``'s output) -- the previous generation's
    entities/relationships for this file are deleted and nothing new is
    written.
    """

    file_id: str
    source_id: str
    generation: int = 0
    clear_only: bool = False
    entities: list[Entity] = field(default_factory=list)
    entity_snippets: dict[str, str] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)


@dataclass(slots=True)
class PreparedDocument:
    """The write-half payload for one document file's sections/
    paragraphs/tables -- what ``documents.pipeline.publish_document``
    hands to ``documents_repo``. ``delete_only`` mirrors
    ``PreparedCode.clear_only``: no content was derived for this file
    (unsupported extension, or an image with OCR off), so only the
    previous generation's rows are cleared.
    """

    file_id: str
    source_id: str
    generation: int = 0
    delete_only: bool = False
    document: Document | None = None
    chunk_ids: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    doc_title: str = ""


@dataclass(slots=True)
class PreparedEmbeddings:
    """The write-half payload for one source pass's embeddings batch --
    already-computed vectors, ready for ``embeddings_repo``/
    ``vector_items_repo``/``embedding_cache_repo`` and the per-file
    embedding-version stamp update, with no model inference left to do
    (see ``indexing.embedding_indexer.prepare_embeddings``, the read/
    inference half this dataclass's values come from).
    """

    source_id: str
    model_id: str = ""
    code_embedding_text_version: str = ""
    document_embedding_text_version: str = ""
    subjects: list[tuple[EmbeddingSubjectType, str, str, str]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    touched_code_file_ids: frozenset[str] = frozenset()
    touched_document_file_ids: frozenset[str] = frozenset()
    cache_entries: list[tuple[str, str, list[float]]] = field(default_factory=list)
    cache_reused: int = 0


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    """A cross-domain link ``knowledge.linker``'s matchers propose,
    before it becomes a stored ``CrossLink`` row. Moved here (Phase 3)
    from ``knowledge/linker.py``, which now imports it from this module
    instead of defining its own copy -- same shape, one definition.
    """

    entity_id: str
    document_id: str
    section_id: str | None
    link_type: RelationshipType
    resolver: str
    confidence: Confidence
    evidence: str


@dataclass(slots=True)
class PreparedLinks:
    """Cross-file/graph links prepared for publish (e.g. import edges,
    symbol references) -- what ``knowledge.linker.link_touched_files``
    hands to ``links_repo.insert`` for each of its matchers' candidates.
    """

    source_id: str
    candidates: list[LinkCandidate] = field(default_factory=list)
    # Completion plan F6: cross-domain links are generation-versioned on
    # server backends too (``None`` = the source's published generation).
    generation: int | None = None


@dataclass(slots=True)
class SearchHit:
    """One backend-neutral search result row."""

    id: str
    score: float
    kind: str = "document"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BackendStats:
    """Aggregate counters a backend can report (``count_stats``)."""

    files: int = 0
    entities: int = 0
    document_units: int = 0
    embeddings: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# -- Completion plan F4: targeted read-contract records --------------------
# Small, backend-neutral projections returned by the targeted read
# primitives added to ``KnowledgeBackend`` (``get_documents``/
# ``list_documents``/``get_document_units``/``get_links``). Every server
# adapter builds these from its own stored payloads, and
# ``LocalKnowledgeBackend`` from its SQLite rows, so a caller (graph
# resolution, impact/explore, the Admin UI) never needs to know which
# backend produced them.


@dataclass(slots=True)
class DocumentRecord:
    """One indexed document's metadata (no chunk bodies)."""

    document_id: str
    source_id: str
    file_id: str
    title: str = ""
    format: str = ""
    author: str | None = None
    page_count: int | None = None
    section_count: int = 0
    paragraph_count: int = 0
    table_count: int = 0
    is_scanned: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class DocumentUnitRecord:
    """One document chunk/section/table unit."""

    unit_id: str
    document_id: str
    file_id: str
    source_id: str
    kind: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    embedding_text: str = ""
    has_embedding: bool = False


@dataclass(slots=True)
class LinkRecord:
    """One stored cross-domain (code entity <-> document) link."""

    entity_id: str
    document_id: str
    section_id: str | None
    link_type: str
    resolver: str
    confidence: str
    evidence: str
    source_id: str = ""
    # Completion plan (server-aware ``ragmonk link``): a stable identifier
    # for this link row -- the local ``CrossLink.id`` uuid in local mode,
    # or the deterministic ``link_key`` hash (see ``opensearch_ids.link_doc_id``
    # / ``elasticsearch_ids``) in server mode. Both are stable across reads
    # of the same underlying row, which is all ``cli/link.py remove`` needs.
    id: str = ""
