"""Graph traversal depth parity between local mode and the server-mode
OpenSearch/Elasticsearch backends.

Before this fix, ``OpenSearchKnowledgeBackend.graph_neighbors``/
``ElasticsearchKnowledgeBackend.graph_neighbors`` already performed real
iterative multi-hop frontier expansion up to ``max_depth`` (see their own
BFS loops), but every returned edge was then tagged ``depth=1`` by the
callers in ``code/graph.py``/``retrieval/graph.py`` regardless of how many
hops it actually took to reach it (see the "Known limitations" note this
fix removes from README.md). These tests build the same
A -> B -> C -> D (-> E) call chain against local SQLite and against both
fake-backed server engines and assert the *same* per-edge hop depths come
back, plus cycle protection, node/edge limits, the unresolved-name CALLS
merge (added after PR #73 CI feedback -- see ``server_common.
find_unresolved_relationships`` / ``retrieval.graph._resolved_server``),
and deterministic ordering regardless of insertion order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit._fake_elasticsearch import FakeElasticsearch
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend
from ragmonk.backends.models import PreparedCode
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.code import graph as code_graph
from ragmonk.core.config import ServerStorageConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import (
    Confidence,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    RelationshipType,
)
from ragmonk.core.models import Relationship as CoreRelationship
from ragmonk.retrieval import graph as retrieval_graph
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import entities_repo, files_repo, relationships_repo
from ragmonk.storage.sqlite import connect

# -- shared fixture data ---------------------------------------------------
#
# Chain: A -> B -> C -> D -> E (CALLS, outgoing from A).
# Cycle: X -> Y -> X (CALLS).
# Unresolved: F calls a symbol "ghost" that is never indexed as an entity
# (target_entity_id is None, target_symbol="ghost") -- the PR #73 merge
# case: still discoverable as an incoming edge on a *resolved* "ghost"
# entity added later, purely by bare name.

_NAMES = ["a", "b", "c", "d", "e", "x", "y", "f", "ghost"]


def _entity(name: str) -> Entity:
    return Entity(
        id=f"ent-{name}",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=f"pkg.{name}",
        language="python",
        start_line=1,
        end_line=2,
        generation=0,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


def _rel(
    rel_id: str,
    source: str,
    *,
    target: str | None = None,
    target_symbol: str | None = None,
) -> CoreRelationship:
    return CoreRelationship(
        id=rel_id,
        relationship_type=RelationshipType.CALLS,
        source_entity_id=f"ent-{source}",
        target_entity_id=f"ent-{target}" if target is not None else None,
        target_symbol=target_symbol,
        resolver="ast",
        confidence=Confidence.EXACT,
        file_id="f1",
        generation=0,
        created_at="2024-01-01T00:00:00Z",
    )


def _chain_relationships(reverse: bool = False) -> list[CoreRelationship]:
    rels = [
        _rel("r-ab", "a", target="b"),
        _rel("r-bc", "b", target="c"),
        _rel("r-cd", "c", target="d"),
        _rel("r-de", "d", target="e"),
        _rel("r-xy", "x", target="y"),
        _rel("r-yx", "y", target="x"),
        _rel("r-f-ghost", "f", target_symbol="ghost"),
    ]
    return list(reversed(rels)) if reverse else rels


def _entities() -> list[Entity]:
    return [_entity(n) for n in _NAMES]


# -- local (sqlite) reference implementation --------------------------------


def _local_chain_conn(tmp_path: Path, *, reverse: bool = False) -> Any:
    conn = connect(tmp_path / "knowledge.db")
    apply_migrations(conn, "knowledge")
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/repo/pkg.py",
            kind=FileKind.CODE,
            size=10,
            mtime=0.0,
            status=FileStatus.INDEXED,
            created_at="now",
            updated_at="now",
        ),
    )
    for entity in _entities():
        entities_repo.insert(conn, entity, snippet=None)
    for rel in _chain_relationships(reverse=reverse):
        relationships_repo.insert(conn, rel)
    return conn


def _local_depths(conn: Any, *, max_depth: int, limit: int = 100) -> dict[str, int]:
    edges = code_graph.traverse(
        conn, "ent-a", direction="outgoing", max_depth=max_depth, limit=limit
    )
    return {e.relationship.target_entity_id: e.depth for e in edges}


# -- server backend builders -------------------------------------------------


def _os_backend() -> OpenSearchKnowledgeBackend:
    config = ServerStorageConfig(engine="opensearch", url="http://fake:9200")  # type: ignore[arg-type]
    return OpenSearchKnowledgeBackend(config, client=FakeOpenSearch())


def _es_backend() -> ElasticsearchKnowledgeBackend:
    config = ServerStorageConfig(engine="elasticsearch", url="http://fake:9200")  # type: ignore[arg-type]
    return ElasticsearchKnowledgeBackend(config, client=FakeElasticsearch())


def _seed_backend(backend: KnowledgeBackend, *, reverse: bool = False) -> None:
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=0,
            entities=_entities(),
            relationships=_chain_relationships(reverse=reverse),
        )
    )


BACKEND_FACTORIES = {"opensearch": _os_backend, "elasticsearch": _es_backend}


@pytest.fixture(params=["opensearch", "elasticsearch"])
def server_backend(request: pytest.FixtureRequest) -> KnowledgeBackend:
    return BACKEND_FACTORIES[request.param]()


def _server_depths(
    backend: KnowledgeBackend, *, max_depth: int, direction: str = "out"
) -> dict[str, int]:
    hits = backend.graph_neighbors("ent-a", direction, max_depth)
    out: dict[str, int] = {}
    for hit in hits:
        target = hit.payload.get("target_entity_id")
        if target and hit.payload.get("source_entity_id") in (
            "ent-a",
            "ent-b",
            "ent-c",
            "ent-d",
        ):
            out[target] = int(hit.payload.get("_hop_depth", 1))
    return out


# -- depth=1/2/3 parity ------------------------------------------------------


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_depth_parity_chain(tmp_path: Path, server_backend: KnowledgeBackend, depth: int) -> None:
    conn = _local_chain_conn(tmp_path)
    try:
        local = _local_depths(conn, max_depth=depth)
    finally:
        conn.close()

    _seed_backend(server_backend)
    server = _server_depths(server_backend, max_depth=depth)

    # Both must report B/C/D at their true hop distance from A, never
    # everything flattened to depth=1 once depth >= 2.
    for name, hop in (("b", 1), ("c", 2), ("d", 3), ("e", 4)):
        if hop > depth:
            assert f"ent-{name}" not in local
            assert f"ent-{name}" not in server
            continue
        assert local[f"ent-{name}"] == hop, f"local depth for {name} at max_depth={depth}"
        assert server[f"ent-{name}"] == hop, f"server depth for {name} at max_depth={depth}"


def test_depth_one_is_still_exactly_one(tmp_path: Path, server_backend: KnowledgeBackend) -> None:
    _seed_backend(server_backend)
    server = _server_depths(server_backend, max_depth=1)
    assert server == {"ent-b": 1}


# -- cycles -------------------------------------------------------------


def test_cycle_does_not_infinite_loop_and_depths_are_correct(
    tmp_path: Path, server_backend: KnowledgeBackend
) -> None:
    conn = _local_chain_conn(tmp_path)
    try:
        local_edges = code_graph.traverse(
            conn, "ent-x", direction="outgoing", max_depth=5, limit=100
        )
    finally:
        conn.close()
    local_depths = {e.relationship.target_entity_id: e.depth for e in local_edges}
    assert local_depths == {"ent-y": 1, "ent-x": 2}

    _seed_backend(server_backend)
    hits = server_backend.graph_neighbors("ent-x", "out", 5)
    depths = {
        hit.payload["target_entity_id"]: int(hit.payload.get("_hop_depth", 1))
        for hit in hits
        if hit.payload.get("source_entity_id") in ("ent-x", "ent-y")
    }
    assert depths == {"ent-y": 1, "ent-x": 2}


# -- node/edge limits at higher depth ----------------------------------------


def test_limit_still_enforced_at_higher_depth(
    ragmonk_home: Path, server_backend: KnowledgeBackend
) -> None:
    _seed_backend(server_backend)
    hits = server_backend.graph_neighbors("ent-a", "out", 3)
    assert len(hits) >= 3  # a->b, b->c, c->d at least reachable

    ctx = _server_ctx_with_backend(ragmonk_home, server_backend)
    try:
        matches, edges = code_graph.traverse_symbol(
            ctx,
            "a",
            direction="outgoing",
            relationship_types=(RelationshipType.CALLS,),
            max_depth=3,
            limit=2,
        )
    finally:
        ctx.close()
    assert len(edges) == 2
    # The deterministic (depth, type, target, id) sort means the two
    # closest edges (depth 1 and 2) win the limit, not an arbitrary
    # frontier-expansion order.
    assert [e.depth for e in edges] == [1, 2]


# -- unresolved-name CALLS merge (PR #73) ------------------------------------


def _server_ctx_with_backend(ragmonk_home: Path, backend: KnowledgeBackend) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    ctx._server_backend = backend
    return ctx


def test_unresolved_calls_merge_preserved_in_resolved_incoming(
    ragmonk_home: Path, server_backend: KnowledgeBackend
) -> None:
    """``f`` calls bare-name ``ghost`` before ``ghost`` existed as a real
    entity (target_entity_id is None, target_symbol="ghost"); once
    ``ghost`` is indexed, ``resolved_incoming`` must still surface that
    edge via ``find_unresolved_relationships`` -- exactly the PR #73
    follow-up behavior -- alongside any depth>1 edges.
    """
    _seed_backend(server_backend)
    ctx = _server_ctx_with_backend(ragmonk_home, server_backend)
    try:
        matches = code_graph.find_symbol_matches(ctx, "ghost")
        assert [m.entity.id for m in matches] == ["ent-ghost"]
        resolved = retrieval_graph.resolved_incoming(
            ctx,
            matches,
            "ghost",
            relationship_types=(RelationshipType.CALLS,),
            max_depth=2,
        )
    finally:
        ctx.close()
    sources = {r.source_id for r in resolved}
    assert "s1" in sources
    unresolved_edges = [
        r for r in resolved if r.edge.relationship.target_entity_id is None
    ]
    assert any(
        r.edge.relationship.target_symbol == "ghost" for r in unresolved_edges
    ), "unresolved f->ghost CALLS edge must still be merged in"


# -- reversed insertion order determinism ------------------------------------


def test_reversed_insertion_order_is_still_deterministic(
    tmp_path: Path, server_backend: KnowledgeBackend
) -> None:
    forward = BACKEND_FACTORIES[
        "opensearch" if isinstance(server_backend, OpenSearchKnowledgeBackend) else "elasticsearch"
    ]()
    _seed_backend(forward)
    forward_hits = forward.graph_neighbors("ent-a", "out", 3)
    forward_depths = sorted(
        (h.payload["target_entity_id"], int(h.payload.get("_hop_depth", 1)))
        for h in forward_hits
        if h.payload.get("target_entity_id")
    )

    _seed_backend(server_backend, reverse=True)
    reverse_hits = server_backend.graph_neighbors("ent-a", "out", 3)
    reverse_depths = sorted(
        (h.payload["target_entity_id"], int(h.payload.get("_hop_depth", 1)))
        for h in reverse_hits
        if h.payload.get("target_entity_id")
    )

    assert forward_depths == reverse_depths

    # And the higher-level, sorted-and-limited server traversal used by
    # code/graph.py's traverse_symbol is itself order-independent too.
    filters = {"relationship_type": [RelationshipType.CALLS.value]}
    forward_edges = forward.graph_neighbors("ent-a", "out", 3, filters)
    reverse_edges = server_backend.graph_neighbors("ent-a", "out", 3, filters)
    key = lambda hits: sorted(  # noqa: E731
        (h.payload.get("source_entity_id"), h.payload.get("target_entity_id"))
        for h in hits
    )
    assert key(forward_edges) == key(reverse_edges)
