"""The ``KnowledgeBackend`` abstract contract.

Storage backend abstraction plan, Phase 1: this is the target interface
retrieval/indexing code will eventually be routed through instead of
calling SQLite-specific repositories directly. That routing is a *future*
phase -- today only :class:`ragmonk.backends.local.LocalKnowledgeBackend`
implements it, as a thin wrapper, and several methods there are
``NotImplementedError`` stubs where the current local code doesn't yet
cleanly map onto this shape (see that module's docstring for which).

Nothing here imports a server-client SDK. A future OpenSearch/Elasticsearch
adapter module should keep those imports inside its own methods (or behind
``TYPE_CHECKING``), never at this module's top level, so importing
``ragmonk.backends.base`` never requires ``opensearch-py``/
``elasticsearch-py`` to be installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)

GraphDirection = Literal["in", "out", "both"]


class KnowledgeBackend(ABC):
    """Backend-neutral storage/retrieval contract.

    A "generation" is a backend-neutral name for the existing local
    rebuild-safety concept (blueprint: begin/publish/abort a versioned
    write generation per source so a reader never sees a half-written
    rebuild). Server adapters are expected to implement it via whatever
    mechanism their engine offers (e.g. an alias swap); that mapping is a
    future phase's job.
    """

    # -- lifecycle -----------------------------------------------------
    @abstractmethod
    def health(self) -> bool:
        """Return whether the backend is reachable and usable."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create/migrate whatever schema this backend needs."""

    @abstractmethod
    def close(self) -> None:
        """Release any held connections/resources."""

    # -- generation (rebuild-safety) lifecycle --------------------------
    @abstractmethod
    def begin_generation(self, source_id: str) -> str:
        """Start a new write generation for ``source_id``, returning its id."""

    @abstractmethod
    def publish_generation(self, source_id: str, generation: str) -> None:
        """Atomically make ``generation`` the visible one for ``source_id``."""

    @abstractmethod
    def abort_generation(self, source_id: str, generation: str) -> None:
        """Discard an in-progress, not-yet-published generation."""

    # -- file/entity/document/embedding/link writes ---------------------
    @abstractmethod
    def upsert_file(self, file_record: FileRecord) -> None: ...

    @abstractmethod
    def delete_file(self, source_id: str, file_id: str) -> None: ...

    @abstractmethod
    def publish_code(self, prepared_code: PreparedCode) -> None: ...

    @abstractmethod
    def publish_document(self, prepared_document: PreparedDocument) -> None: ...

    @abstractmethod
    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None: ...

    @abstractmethod
    def publish_links(self, prepared_links: PreparedLinks) -> None: ...

    # -- reads / search ---------------------------------------------------
    @abstractmethod
    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]: ...

    @abstractmethod
    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]: ...

    @abstractmethod
    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]: ...

    @abstractmethod
    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]: ...

    @abstractmethod
    def get_file(self, file_id: str) -> FileRecord | None: ...

    @abstractmethod
    def get_entities_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_document_units_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def count_stats(self) -> BackendStats: ...

    @abstractmethod
    def clear_source(self, source_id: str) -> None: ...
