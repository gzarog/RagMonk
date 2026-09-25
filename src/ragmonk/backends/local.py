"""``LocalKnowledgeBackend`` -- a thin ``KnowledgeBackend`` wrapper around
the existing local SQLite+FTS5+USearch stack.

Storage backend abstraction plan, Phase 1: this class does NOT yet reroute
any of today's indexing/retrieval code through the ``KnowledgeBackend``
contract -- that's a future phase. It exists so the factory has a real,
working "local" backend object to return, and so future backend-contract
tests have something to run against for local mode. Several methods below
that do not yet cleanly map onto today's SQLite repository layer (which is
organized per-repository, not behind one unified interface) are explicit
``NotImplementedError`` stubs -- noted inline -- rather than fake
implementations.

Imports here stay within the existing local stack
(``ragmonk.storage``/``ragmonk.core.paths``); nothing server-specific is
imported, directly or transitively.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)
from ragmonk.core import paths
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect


class LocalKnowledgeBackend(KnowledgeBackend):
    """Wraps the local SQLite ``sources.db`` connection/migration
    lifecycle. Constructed with the same ``home`` directory the rest of
    the local stack (``ragmonk.core.paths``) already resolves against.
    """

    def __init__(self, *, home: Path | None = None, cache_size_mb: int = 64) -> None:
        self._home = home or paths.runtime_dir()
        self._cache_size_mb = cache_size_mb
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        """Real, minimal check: open (or reuse) the sources.db connection
        and run a trivial query.
        """
        try:
            conn = self._connection()
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def ensure_schema(self) -> None:
        conn = self._connection()
        apply_migrations(conn, "sources")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(
                paths.sources_db_path(self._home), cache_size_mb=self._cache_size_mb
            )
        return self._conn

    # -- generation lifecycle -------------------------------------------
    # Not yet mapped: today's local rebuild-safety story is implemented
    # per-repository inside ``indexing/coordinator.py`` and
    # ``storage/repositories``, not as a single named "generation" handle.
    # Reusing that machinery here is a future phase's job.
    def begin_generation(self, source_id: str) -> str:
        raise NotImplementedError(
            "LocalKnowledgeBackend.begin_generation: local rebuild-safety is not yet "
            "wired through the KnowledgeBackend contract (future phase)"
        )

    def publish_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_generation: not yet wired (future phase)"
        )

    def abort_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.abort_generation: not yet wired (future phase)"
        )

    # -- writes -----------------------------------------------------------
    # Not yet mapped onto this contract: today's writes go through
    # ``storage/repositories/*`` directly from ``indexing/coordinator.py``
    # and ``code/processor.py``/``documents`` pipelines, keyed by
    # repository-specific row shapes rather than these neutral dataclasses.
    def upsert_file(self, file_record: FileRecord) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.upsert_file: not yet wired (future phase)"
        )

    def delete_file(self, source_id: str, file_id: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.delete_file: not yet wired (future phase)"
        )

    def publish_code(self, prepared_code: PreparedCode) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_code: not yet wired (future phase)"
        )

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_document: not yet wired (future phase)"
        )

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_embeddings: not yet wired (future phase)"
        )

    def publish_links(self, prepared_links: PreparedLinks) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_links: not yet wired (future phase)"
        )

    # -- reads / search ---------------------------------------------------
    # Not yet mapped: today's search entry points live in
    # ``retrieval/lexical.py``/``semantic.py``/``graph.py`` and return
    # their own result types, not ``SearchHit``. Routing them through here
    # is a future phase.
    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.lexical_search: not yet wired (future phase); "
            "use ragmonk.retrieval.lexical directly for now"
        )

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.semantic_search: not yet wired (future phase); "
            "use ragmonk.retrieval.semantic directly for now"
        )

    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.symbol_search: not yet wired (future phase)"
        )

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.graph_neighbors: not yet wired (future phase); "
            "use ragmonk.retrieval.graph directly for now"
        )

    def get_file(self, file_id: str) -> FileRecord | None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_file: not yet wired (future phase)"
        )

    def get_entities_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_entities_for_files: not yet wired (future phase)"
        )

    def get_document_units_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_document_units_for_files: not yet wired "
            "(future phase)"
        )

    def count_stats(self) -> BackendStats:
        raise NotImplementedError(
            "LocalKnowledgeBackend.count_stats: not yet wired (future phase)"
        )

    def clear_source(self, source_id: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.clear_source: not yet wired (future phase)"
        )
