"""The ``KnowledgeBackend`` abstract contract.

Storage backend abstraction plan: implemented by
:class:`ragmonk.backends.local.LocalKnowledgeBackend` (local SQLite; its
search/stats methods remain ``NotImplementedError`` because local-mode
retrieval reads SQLite directly), and by the OpenSearch and Elasticsearch
adapters, through which *all* server-mode indexing and retrieval flows.
Completion plan F4 added the targeted read primitives at the bottom of the
class (entity/file/link/document reads by id), implemented by all three.

Nothing here imports a server-client SDK. A future OpenSearch/Elasticsearch
adapter module should keep those imports inside its own methods (or behind
``TYPE_CHECKING``), never at this module's top level, so importing
``ragmonk.backends.base`` never requires ``opensearch-py``/
``elasticsearch-py`` to be installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from ragmonk.backends.models import (
    BackendStats,
    DocumentRecord,
    DocumentUnitRecord,
    FileRecord,
    LinkRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ragmonk.core.models import Entity

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
    def publish_links(self, prepared_links: PreparedLinks) -> int:
        """Writes ``prepared_links``'s candidates as ``CrossLink`` rows,
        deduplicating exactly like the pre-Phase-3 ``knowledge.linker._store``
        did (a link's natural-key uniqueness is enforced by the storage
        layer, not here), and returns how many were newly inserted --
        needed by ``knowledge.linker.link_touched_files``'s own return
        value/telemetry, which a plain ``None`` can't carry.
        """

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
    def get_entities_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        """``generation`` (completion plan F1/F6): a server backend reads
        exactly that write generation instead of the published one -- used
        only by the indexing pass writing it. Ignored by local mode."""

    @abstractmethod
    def get_document_units_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        """See ``get_entities_for_files`` for ``generation``."""

    @abstractmethod
    def count_stats(self) -> BackendStats: ...

    @abstractmethod
    def clear_source(self, source_id: str) -> None: ...

    # -- Completion plan F4: targeted read primitives ----------------------
    # Concrete (not abstract) so existing test doubles that only implement
    # the original contract keep working; every *real* backend
    # (``LocalKnowledgeBackend``, ``OpenSearchKnowledgeBackend``,
    # ``ElasticsearchKnowledgeBackend``) overrides all of them -- enforced
    # by ``tests/unit/test_backend_read_contract.py``. A server backend
    # answers every one of these from the server engine itself, filtered
    # to each source's published generation -- never from local SQLite.

    @property
    def is_server(self) -> bool:
        """True for a remote search-engine backend (searchable knowledge
        lives outside local SQLite)."""
        return False

    def published_generation(self, source_id: str) -> str | None:
        """The source's currently published generation id, or ``None``
        if nothing has ever been published for it (local mode: always
        ``None`` -- local rebuild safety is file-backup based)."""
        return None

    def upsert_files(self, file_records: list[FileRecord]) -> None:
        for record in file_records:
            self.upsert_file(record)

    def get_files(self, file_ids: list[str]) -> list[FileRecord]:
        raise NotImplementedError(f"{type(self).__name__}.get_files")

    def list_files(self, source_id: str) -> list[FileRecord]:
        raise NotImplementedError(f"{type(self).__name__}.list_files")

    def get_entities(self, entity_ids: list[str]) -> list[Entity]:
        raise NotImplementedError(f"{type(self).__name__}.get_entities")

    def list_entities(
        self, *, source_id: str | None = None, query: str | None = None, limit: int = 100
    ) -> list[Entity]:
        raise NotImplementedError(f"{type(self).__name__}.list_entities")

    def list_source_entities(
        self, source_id: str, *, generation: str | None = None
    ) -> list[Entity]:
        raise NotImplementedError(f"{type(self).__name__}.list_source_entities")

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        raise NotImplementedError(f"{type(self).__name__}.find_entities_by_names")

    def get_links(
        self,
        *,
        entity_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[LinkRecord]:
        raise NotImplementedError(f"{type(self).__name__}.get_links")

    def get_documents(self, document_ids: list[str]) -> list[DocumentRecord]:
        raise NotImplementedError(f"{type(self).__name__}.get_documents")

    def list_documents(
        self, *, source_id: str | None = None, limit: int | None = None
    ) -> list[DocumentRecord]:
        raise NotImplementedError(f"{type(self).__name__}.list_documents")

    def get_document_units(
        self,
        *,
        document_id: str | None = None,
        unit_ids: list[str] | None = None,
    ) -> list[DocumentUnitRecord]:
        raise NotImplementedError(f"{type(self).__name__}.get_document_units")

    def list_source_document_units(
        self, source_id: str, *, generation: str | None = None
    ) -> list[DocumentUnitRecord]:
        raise NotImplementedError(f"{type(self).__name__}.list_source_document_units")

    def find_relationships_by_target_prefix(
        self, source_id: str, prefix: str, *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(f"{type(self).__name__}.find_relationships_by_target_prefix")
