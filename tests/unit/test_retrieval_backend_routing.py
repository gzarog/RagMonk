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
from ragmonk.core.errors import LocalStorageModeRequiredError
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
        symbol_hits: list[SearchHit] | None = None,
        neighbor_hits: dict[tuple[str, str], list[SearchHit]] | None = None,
    ) -> None:
        self._lexical_hits = lexical_hits or []
        self._semantic_hits = semantic_hits or []
        self._raise_on_lexical = raise_on_lexical
        self._symbol_hits = symbol_hits or []
        self._neighbor_hits = neighbor_hits or {}
        self.lexical_calls = 0
        self.semantic_calls = 0
        self.symbol_calls = 0
        self.graph_neighbor_calls: list[tuple[str, str]] = []
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
        self.symbol_calls += 1
        return list(self._symbol_hits)

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        self.graph_neighbor_calls.append((entity_id, direction))
        return list(self._neighbor_hits.get((entity_id, direction), []))

    # Completion plan F4 targeted reads -- in-memory, keyed by id.
    entities_by_id: dict[str, Any] = {}
    files_by_id: dict[str, FileRecord] = {}
    links: list[Any] = []
    documents_by_id: dict[str, Any] = {}
    units_by_id: dict[str, Any] = {}

    def get_entities(self, entity_ids: list[str]) -> list[Any]:
        return [self.entities_by_id[i] for i in entity_ids if i in self.entities_by_id]

    def get_files(self, file_ids: list[str]) -> list[FileRecord]:
        return [self.files_by_id[i] for i in file_ids if i in self.files_by_id]

    def get_links(
        self, *, entity_ids: list[str] | None = None, document_ids: list[str] | None = None
    ) -> list[Any]:
        wanted = set(entity_ids or [])
        return [lk for lk in self.links if lk.entity_id in wanted]

    def get_documents(self, document_ids: list[str]) -> list[Any]:
        return [self.documents_by_id[i] for i in document_ids if i in self.documents_by_id]

    def get_document_units(
        self, *, document_id: str | None = None, unit_ids: list[str] | None = None
    ) -> list[Any]:
        return [self.units_by_id[i] for i in unit_ids or [] if i in self.units_by_id]

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
        with pytest.raises(LocalStorageModeRequiredError, match="storage.mode"):
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


def _entity_payload(entity_id: str, name: str, *, source_id: str = "s1") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "source_id": source_id,
        "file_id": "f1",
        "kind": "function",
        "name": name,
        "qualified_name": f"pkg.{name}",
        "language": "python",
        "snippet": f"def {name}(): ...",
        "start_line": 1,
        "end_line": 2,
        "generation": "1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _relationship_payload(
    *, source_entity_id: str, target_entity_id: str | None, rel_type: str = "calls"
) -> dict[str, Any]:
    return {
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "target_symbol": None,
        "resolver": "ast",
        "confidence": "exact",
        "file_id": "f1",
        "evidence": None,
        "generation": "1",
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_find_symbol_matches_routes_through_backend_symbol_search(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = SearchHit(id="s1:f1:e1", score=1.0, kind="entity", payload=_entity_payload("e1", "Foo"))
    stub = _StubBackend(symbol_hits=[hit])
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local sqlite must never run for server-mode symbol lookup")

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    try:
        matches = code_graph.find_symbol_matches(ctx, "Foo")
    finally:
        ctx.close()

    assert stub.symbol_calls == 1
    assert [m.entity.id for m in matches] == ["e1"]
    assert matches[0].entity.qualified_name == "pkg.Foo"


def test_traverse_symbol_routes_through_backend_graph_neighbors(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    symbol_hit = SearchHit(
        id="s1:f1:e1", score=1.0, kind="entity", payload=_entity_payload("e1", "Foo")
    )
    neighbor_hit = SearchHit(
        id="s1:f1:r1",
        score=1.0,
        kind="relationship",
        payload=_relationship_payload(source_entity_id="e2", target_entity_id="e1"),
    )
    stub = _StubBackend(symbol_hits=[symbol_hit], neighbor_hits={("e1", "in"): [neighbor_hit]})
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local sqlite must never run for server-mode traverse_symbol")

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    from ragmonk.core.models import RelationshipType

    try:
        matches, edges = code_graph.traverse_symbol(
            ctx,
            "Foo",
            direction="incoming",
            relationship_types=(RelationshipType.CALLS,),
        )
    finally:
        ctx.close()

    assert [m.entity.id for m in matches] == ["e1"]
    assert stub.graph_neighbor_calls == [("e1", "in")]
    assert len(edges) == 1
    assert edges[0].relationship.source_entity_id == "e2"
    assert edges[0].relationship.target_entity_id == "e1"


def test_references_routes_through_backend_both_directions(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.retrieval import graph as retrieval_graph

    symbol_hit = SearchHit(
        id="s1:f1:e1", score=1.0, kind="entity", payload=_entity_payload("e1", "Foo")
    )
    in_hit = SearchHit(
        id="s1:f1:rin",
        score=1.0,
        kind="relationship",
        payload=_relationship_payload(source_entity_id="e2", target_entity_id="e1"),
    )
    out_hit = SearchHit(
        id="s1:f1:rout",
        score=1.0,
        kind="relationship",
        payload=_relationship_payload(source_entity_id="e1", target_entity_id="e3"),
    )
    stub = _StubBackend(
        symbol_hits=[symbol_hit],
        neighbor_hits={("e1", "in"): [in_hit], ("e1", "out"): [out_hit]},
    )
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local sqlite must never run for server-mode references")

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    try:
        matches, edges = retrieval_graph.references(ctx, "Foo")
    finally:
        ctx.close()

    assert [m.entity.id for m in matches] == ["e1"]
    assert set(stub.graph_neighbor_calls) == {("e1", "in"), ("e1", "out")}
    assert len(edges) == 2
    pairs = {(e.relationship.source_entity_id, e.relationship.target_entity_id) for e in edges}
    assert pairs == {("e2", "e1"), ("e1", "e3")}


def test_resolved_incoming_outgoing_resolve_neighbors_through_backend(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion plan F4 (replaces the former "no neighbor resolution"
    limitation test): server-mode ``ResolvedEdge`` rows carry the real
    neighbor ``Entity``/``FileRecord``, resolved through the backend's
    ``get_entities``/``get_files`` -- never local sqlite -- and the
    tests signal works because the caller's file is known.
    """
    from ragmonk.core.models import RelationshipType
    from ragmonk.retrieval import graph as retrieval_graph

    symbol_hit = SearchHit(
        id="s1:f1:e1", score=1.0, kind="entity", payload=_entity_payload("e1", "Foo")
    )
    in_hit = SearchHit(
        id="s1:f1:rin",
        score=1.0,
        kind="relationship",
        payload=_relationship_payload(source_entity_id="e2", target_entity_id="e1"),
    )
    out_hit = SearchHit(
        id="s1:f1:rout",
        score=1.0,
        kind="relationship",
        payload=_relationship_payload(source_entity_id="e1", target_entity_id="e3"),
    )
    stub = _StubBackend(
        symbol_hits=[symbol_hit],
        neighbor_hits={("e1", "in"): [in_hit], ("e1", "out"): [out_hit]},
    )
    caller = code_graph.entity_from_symbol_hit(
        SearchHit(id="x", score=1.0, kind="entity", payload={
            **_entity_payload("e2", "test_foo"), "file_id": "ftest"
        })
    )
    callee = code_graph.entity_from_symbol_hit(
        SearchHit(id="y", score=1.0, kind="entity", payload=_entity_payload("e3", "Bar"))
    )
    stub.entities_by_id = {"e2": caller, "e3": callee}
    stub.files_by_id = {
        "ftest": FileRecord(file_id="ftest", source_id="s1", path="tests/test_foo.py",
                            content_hash="h"),
        "f1": FileRecord(file_id="f1", source_id="s1", path="pkg/foo.py", content_hash="h"),
    }
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local sqlite must never run for server-mode resolved edges")

    monkeypatch.setattr(code_graph, "all_project_connections", _boom)
    monkeypatch.setattr(code_graph, "conn_for_source_path", _boom)
    try:
        matches = code_graph.find_symbol_matches(ctx, "Foo")
        callers = retrieval_graph.resolved_incoming(
            ctx, matches, "Foo", relationship_types=(RelationshipType.CALLS,)
        )
        callees = retrieval_graph.resolved_outgoing(
            ctx, matches, relationship_types=(RelationshipType.CALLS,)
        )
        tests = retrieval_graph.find_tests_referencing(ctx, matches, "Foo")
    finally:
        ctx.close()

    assert len(callers) == 1
    assert callers[0].neighbor_entity is not None and callers[0].neighbor_entity.id == "e2"
    assert callers[0].neighbor_file is not None
    assert callers[0].neighbor_file.path == "tests/test_foo.py"
    assert len(callees) == 1
    assert callees[0].neighbor_entity is not None and callees[0].neighbor_entity.name == "Bar"
    assert callees[0].neighbor_file is not None and callees[0].neighbor_file.path == "pkg/foo.py"
    assert [t.neighbor_entity.id for t in tests if t.neighbor_entity] == ["e2"]


def test_impact_and_explore_include_doc_links_in_server_mode(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion plan F4 (replaces the former "skip doc links" limitation
    test): ``impact``/``explore`` read cross-domain links, their documents,
    units and files through the backend in server mode, reporting the
    same documentation evidence local mode does -- never local sqlite.
    """
    from ragmonk.backends.models import DocumentRecord, DocumentUnitRecord, LinkRecord
    from ragmonk.cli import explore as explore_cli
    from ragmonk.cli import impact as impact_cli
    from ragmonk.code.graph import SourceMatch
    from ragmonk.retrieval import planner

    entity = code_graph.entity_from_symbol_hit(
        SearchHit(id="s1:f1:e1", score=1.0, kind="entity", payload=_entity_payload("e1", "Foo"))
    )
    match = SourceMatch(source_id="s1", source_path="s1", entity=entity)

    stub = _StubBackend()
    stub.files_by_id = {
        "f1": FileRecord(file_id="f1", source_id="s1", path="pkg/foo.py", content_hash="h"),
        "fd": FileRecord(file_id="fd", source_id="s1", path="docs/guide.md", content_hash="h"),
    }
    stub.links = [
        LinkRecord(
            entity_id="e1", document_id="d1", section_id="u1", link_type="documented_by",
            resolver="exact_identifier", confidence="exact", evidence="Foo", source_id="s1",
        )
    ]
    stub.documents_by_id = {
        "d1": DocumentRecord(document_id="d1", source_id="s1", file_id="fd", title="Guide")
    }
    stub.units_by_id = {
        "u1": DocumentUnitRecord(
            unit_id="u1", document_id="d1", file_id="fd", source_id="s1", kind="paragraph",
            text="Foo does things", heading_path=["Guide", "Foo"], page_start=None,
        )
    }
    ctx = _server_ctx(ragmonk_home, stub)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local sqlite must never run for server-mode doc-link lookups")

    monkeypatch.setattr(code_graph, "conn_for_source_path", _boom)
    monkeypatch.setattr("ragmonk.knowledge.document_links.conn_for_source_path", _boom)
    monkeypatch.setattr("ragmonk.cli.impact.conn_for_source_path", _boom)
    try:
        defined = impact_cli._defined_locations(ctx, [match])
        assert defined[0]["path"] == "pkg/foo.py"
        documents, confidences = impact_cli._documentation(ctx, [match])
        assert [d["path"] for d in documents] == ["docs/guide.md"]
        assert documents[0]["location"]["section"] == "Guide > Foo"
        assert [c.value for c in confidences] == ["exact"]
        evidence = explore_cli._document_links(
            ctx, [match], strategies=(planner.Strategy.DOCUMENTS,)
        )
        assert [e.path for e in evidence] == ["docs/guide.md"]
    finally:
        ctx.close()
