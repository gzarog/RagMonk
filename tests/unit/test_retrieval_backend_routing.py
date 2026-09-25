"""Storage backend abstraction plan, Phase 6: retrieval routing.

Covers:
- ``AppContext.backend()`` construction/caching for both modes.
- ``all_project_connections`` refusing to enumerate local per-project
  sqlite databases outside local mode (the single choke point behind
  lexical/semantic/symbol retrieval's "no silent local fallback" rule).
- ``retrieval.lexical.search_with_timings``/``retrieval.semantic.
  semantic_search`` routing through ``ctx.backend()`` in server mode,
  and never touching local sqlite even when the server backend call
  itself fails.
- Result schema stability: the same ``SearchResult``/``SemanticHit``
  ``to_dict()`` keys regardless of which mode produced them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.backends.models import BackendStats, FileRecord, SearchHit
from ragmonk.code import graph as code_graph
from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import lexical, semantic


class _StubBackend(KnowledgeBackend):
    """A minimal, in-memory ``KnowledgeBackend`` double -- stands in for a
    real OpenSearch/Elasticsearch adapter so these tests never need a
    live server, exactly like P4/P5's own fakes do for the adapters
    themselves.
    """

    def __init__(
        self,
        *,
        lexical_hits: list[SearchHit] | None = None,
        semantic_hits: list[SearchHit] | None = None,
        raise_on_lexical: bool = False,
    ) -> None:
        self._lexical_hits = lexical_hits or []
        self._semantic_hits = semantic_hits or []
        self._raise_on_lexical = raise_on_lexical
        self.lexical_calls = 0
        self.semantic_calls = 0
        self.closed = False

    def health(self) -> bool:
        return True

    def ensure_schema(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def begin_generation(self, source_id: str) -> str:
        raise NotImplementedError

    def publish_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError

    def abort_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError

    def upsert_file(self, file_record: FileRecord) -> None:
        raise NotImplementedError

    def delete_file(self, source_id: str, file_id: str) -> None:
        raise NotImplementedError

    def publish_code(self, prepared_code: Any) -> None:
        raise NotImplementedError

    def publish_document(self, prepared_document: Any) -> None:
        raise NotImplementedError

    def publish_embeddings(self, prepared_embeddings: Any) -> None:
        raise NotImplementedError

    def publish_links(self, prepared_links: Any) -> int:
        raise NotImplementedError

    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        self.lexical_calls += 1
        if self._raise_on_lexical:
            raise RuntimeError("simulated server outage")
        return self._lexical_hits[:limit]

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        self.semantic_calls += 1
        return self._semantic_hits[:limit]

    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError

    def get_file(self, file_id: str) -> FileRecord | None:
        raise NotImplementedError

    def get_entities_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_document_units_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count_stats(self) -> BackendStats:
        raise NotImplementedError

    def clear_source(self, source_id: str) -> None:
        raise NotImplementedError


def _server_ctx(ragmonk_home: Path, stub: KnowledgeBackend) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    # Inject the stub in place of a real OpenSearch/Elasticsearch client so
    # these tests exercise the routing/no-fallback contract without a live
    # server -- ``AppContext.backend()`` caches whatever is already set.
    ctx._server_backend = stub
    return ctx


def test_app_context_backend_local_mode_wraps_project_conn(ragmonk_home: Path) -> None:
    with AppContext.bootstrap() as ctx:
        assert ctx.config.storage.mode == "local"
        backend = ctx.backend("proj1")
        assert isinstance(backend, LocalKnowledgeBackend)
        with pytest.raises(ValueError):
            ctx.backend(None)


def test_app_context_backend_server_mode_is_cached_singleton(ragmonk_home: Path) -> None:
    stub = _StubBackend()
    ctx = _server_ctx(ragmonk_home, stub)
    try:
        assert ctx.backend() is stub
        assert ctx.backend("irrelevant-project-id") is stub
    finally:
        ctx.close()
    assert stub.closed


def test_all_project_connections_refuses_server_mode(ragmonk_home: Path) -> None:
    stub = _StubBackend()
    ctx = _server_ctx(ragmonk_home, stub)
    try:
        with pytest.raises(RuntimeError, match="storage.mode"):
            code_graph.all_project_connections(ctx)
    finally:
        ctx.close()


def test_lexical_search_routes_through_backend_and_never_touches_sqlite(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = SearchHit(
        id="e1",
        score=3.0,
        kind="entity",
        payload={"qualified_name": "pkg.Foo", "path": "/repo/foo.py", "source_id": "s1"},
    )
    stub = _StubBackend(lexical_hits=[hit])
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "local sqlite enumeration must never run for server-mode lexical search"
        )

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    monkeypatch.setattr(lexical, "all_project_connections", _boom)
    try:
        timed = lexical.search_with_timings(ctx, "Foo", limit=10)
    finally:
        ctx.close()

    assert stub.lexical_calls == 1
    assert [r.id for r in timed.results] == ["e1"]
    assert timed.results[0].to_dict().keys() == {
        "kind",
        "tier",
        "id",
        "title",
        "path",
        "source_id",
        "snippet",
        "location",
    }


def test_semantic_search_routes_through_backend(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = SearchHit(
        id="e1",
        score=0.9,
        kind="entity",
        payload={"name": "Foo", "path": "/repo/foo.py", "source_id": "s1"},
    )
    stub = _StubBackend(semantic_hits=[hit])
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "local sqlite enumeration must never run for server-mode semantic search"
        )

    monkeypatch.setattr(semantic, "all_project_connections", _boom)
    from ragmonk.core.config import SearchConfig

    config = SearchConfig(semantic=True)
    try:
        result = semantic.semantic_search(ctx, "Foo", config=config, limit=5)
    finally:
        ctx.close()

    assert stub.semantic_calls == 1
    assert result.available is True
    assert [h.id for h in result.results] == ["e1"]
    assert result.results[0].to_dict().keys() == {
        "kind",
        "tier",
        "id",
        "title",
        "path",
        "source_id",
        "snippet",
        "location",
        "score",
    }


def test_server_backend_failure_raises_never_falls_back_to_local(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's explicit "never" rule: if the server backend errors, the
    caller must see that error -- not a quiet, empty/degraded local
    result. This is exactly what the review checklist's "silent fallback
    from server mode to local knowledge search" question is asking about.
    """
    stub = _StubBackend(raise_on_lexical=True)
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("must not fall back to local sqlite on a server backend error")

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    monkeypatch.setattr(lexical, "all_project_connections", _boom)
    try:
        with pytest.raises(RuntimeError, match="simulated server outage"):
            lexical.search_with_timings(ctx, "Foo", limit=10)
    finally:
        ctx.close()
    assert stub.lexical_calls == 1
