"""Backend-neutral data models for the ``KnowledgeBackend`` contract.

Storage backend abstraction plan, Phase 1: these are deliberately minimal
stubs. Full wiring of the existing SQLite-shaped records (code entities,
document chunks, embeddings, graph links, file records) onto these
dataclasses is a future phase's job -- today they exist only so
``base.py``'s method signatures have a concrete, backend-neutral type to
name instead of a raw ``dict`` or a SQLite-specific row type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass(slots=True)
class PreparedCode:
    """The prepare-half output of code extraction (Tree-sitter parse +
    entity/relationship extraction) for one file, ready to publish.
    """

    file_id: str
    source_id: str
    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PreparedDocument:
    """The prepare-half output of document conversion + chunking for one
    file, ready to publish.
    """

    file_id: str
    source_id: str
    units: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PreparedEmbeddings:
    """Embedding vectors prepared for one file's chunks/entities, ready to
    publish into a backend's vector index.
    """

    file_id: str
    source_id: str
    vectors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PreparedLinks:
    """Cross-file/graph links prepared for publish (e.g. import edges,
    symbol references).
    """

    source_id: str
    links: list[dict[str, Any]] = field(default_factory=list)


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
