"""Shared, DB-level graph traversal backing the Phase 2 read-only CLI
commands (``symbol``/``callers``/``callees``/``references``).

Every traversal takes an explicit ``max_depth`` and ``limit`` and returns
a deterministically ordered result, even though today's callers only ever
walk one hop: Phase 5's ``impact`` command is expected to reuse this same
BFS rather than growing its own, so the knobs are here from the start.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ragmonk.backends.base import GraphDirection
from ragmonk.backends.models import SearchHit
from ragmonk.core import paths
from ragmonk.core.errors import LocalStorageModeRequiredError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, Entity, EntityType, Relationship, RelationshipType
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import entities_repo, relationships_repo

DEFAULT_MAX_DEPTH = 1
DEFAULT_LIMIT = 100

Direction = Literal["incoming", "outgoing"]

_DIRECTION_TO_BACKEND: dict[Direction, GraphDirection] = {"incoming": "in", "outgoing": "out"}


@dataclass(frozen=True)
class TraversalEdge:
    depth: int
    relationship: Relationship


@dataclass(frozen=True)
class SourceMatch:
    source_id: str
    source_path: str
    entity: Entity


def _sort_key(relationship: Relationship) -> tuple[str, str, str]:
    return (
        relationship.relationship_type.value,
        relationship.target_entity_id or relationship.target_symbol or "",
        relationship.id,
    )


def traverse(
    conn: sqlite3.Connection,
    start_entity_id: str,
    *,
    direction: Direction,
    relationship_types: tuple[RelationshipType, ...] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> list[TraversalEdge]:
    fetch = relationships_repo.incoming if direction == "incoming" else relationships_repo.outgoing
    visited = {start_entity_id}
    frontier = [start_entity_id]
    results: list[TraversalEdge] = []
    depth = 1
    while frontier and depth <= max_depth and len(results) < limit:
        next_frontier: list[str] = []
        for entity_id in frontier:
            if relationship_types:
                edges: list[Relationship] = []
                for rel_type in relationship_types:
                    edges.extend(fetch(conn, entity_id, relationship_type=rel_type, limit=limit))
            else:
                edges = fetch(conn, entity_id, limit=limit)
            edges.sort(key=_sort_key)
            for edge in edges:
                results.append(TraversalEdge(depth=depth, relationship=edge))
                if len(results) >= limit:
                    break
                neighbor = (
                    edge.target_entity_id if direction == "outgoing" else edge.source_entity_id
                )
                if neighbor is not None and neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
            if len(results) >= limit:
                break
        frontier = next_frontier
        depth += 1
    return results[:limit]


def all_project_connections(ctx: AppContext) -> list[tuple[str, str, sqlite3.Connection]]:
    """(source_id, source_path, conn) for every registered source.

    Storage backend abstraction plan, Phase 6: this is the one place that
    enumerates local per-project ``knowledge.db`` files, and every retrieval
    call site that used to open sqlite directly (``retrieval/lexical.py``,
    ``retrieval/semantic.py``, ``find_symbol_matches``/``traverse_symbol``
    below) still funnels through it for local mode. That makes it the
    single choke point for this phase's "never silently fall back to local
    SQLite in server mode" rule: raising here, rather than in each of those
    call sites individually, is what guarantees none of them can
    accidentally enumerate local databases when ``storage.mode == "server"``
    -- a server backend is the single source of truth across sources, and
    this per-project-sqlite pattern is meaningless for it.
    """
    if ctx.config.storage.mode != "local":
        raise LocalStorageModeRequiredError(
            "all_project_connections: storage.mode is "
            f"{ctx.config.storage.mode!r}, not 'local' -- local per-project "
            "sqlite enumeration must never run outside local mode (no "
            "silent local fallback in server mode); callers must route "
            "through ctx.backend() instead"
        )
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    return [
        (source.id, source.path, ctx.project_conn(paths.project_id_for_path(Path(source.path))))
        for source in registry.list()
    ]


def entity_from_symbol_hit(hit: SearchHit) -> Entity:
    """Reconstructs an ``Entity`` from a backend-neutral ``SearchHit`` as
    returned by ``KnowledgeBackend.symbol_search``/``graph_neighbors``.

    ``hit.id`` is the backend's own composite document id (e.g.
    ``source_id:file_id:entity_id`` for the OpenSearch/Elasticsearch
    adapters), never the entity's own id -- the real entity id is carried
    separately in ``payload["entity_id"]`` (see ``backends/opensearch.py``'s
    ``publish_code``). ``signature``/``parent_id``/``start_col``/``end_col``
    have no equivalent field in the indexed payload (only ``snippet``,
    ``start_line``, ``end_line`` are stored), so they fall back to
    ``Entity``'s own defaults (``None``/``0``) exactly like a local-mode
    row would for an entity with no recorded signature.
    """
    payload = hit.payload
    return Entity(
        id=str(payload.get("entity_id") or hit.id),
        source_id=str(payload.get("source_id", "")),
        file_id=str(payload.get("file_id", "")),
        kind=EntityType(payload["kind"]),
        name=str(payload["name"]),
        qualified_name=str(payload["qualified_name"]),
        language=str(payload.get("language", "")),
        signature=payload.get("snippet"),
        start_line=int(payload.get("start_line") or 0),
        end_line=int(payload.get("end_line") or 0),
        generation=int(payload.get("generation") or 0),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def source_match_from_symbol_hit(hit: SearchHit) -> SourceMatch:
    """``source_path`` has no server-mode equivalent (a server backend is
    one source of truth across every registered source, not a per-project
    sqlite file -- see ``all_project_connections``'s docstring), so it is
    set to ``source_id`` itself: informational only, and deliberately
    never fed to ``conn_for_source_path`` by any server-mode code path in
    this module (that would silently open local sqlite in server mode,
    exactly what Phase 6 guards against).
    """
    source_id = str(hit.payload.get("source_id", ""))
    return SourceMatch(
        source_id=source_id, source_path=source_id, entity=entity_from_symbol_hit(hit)
    )


def relationship_from_search_hit(hit: SearchHit) -> Relationship:
    """Reconstructs a ``Relationship`` from a ``graph_neighbors`` hit.

    ``hit.id`` is again the backend's composite document id, not the
    relationship's own id (never stored as its own payload field by the
    OpenSearch/Elasticsearch adapters -- see ``publish_code``); it is only
    ever used downstream for de-duplication and dict rendering, neither of
    which needs the original UUID, so reusing it here is safe.
    ``source_location`` has no payload field either and falls back to
    ``None``, same as an unrecorded local row would.
    """
    payload = hit.payload
    return Relationship(
        id=str(hit.id),
        relationship_type=RelationshipType(payload["relationship_type"]),
        source_entity_id=str(payload.get("source_entity_id") or ""),
        target_entity_id=payload.get("target_entity_id"),
        target_symbol=payload.get("target_symbol"),
        resolver=str(payload.get("resolver", "")),
        confidence=Confidence(payload["confidence"]),
        file_id=str(payload.get("file_id", "")),
        source_location=payload.get("source_location"),
        evidence=payload.get("evidence"),
        generation=int(payload.get("generation") or 0),
        created_at=str(payload.get("created_at", "")),
    )


def _find_symbol_matches_server(ctx: AppContext, name: str) -> list[SourceMatch]:
    hits = ctx.backend().symbol_search(name)
    matches = [source_match_from_symbol_hit(hit) for hit in hits]
    matches.sort(key=lambda m: (m.entity.qualified_name, m.source_id, m.entity.id))
    return matches


def find_symbol_matches(ctx: AppContext, name: str) -> list[SourceMatch]:
    """Every entity across every registered source matching ``name``
    exactly by bare name or qualified name, deterministically ordered.
    """
    if ctx.config.storage.mode == "server":
        return _find_symbol_matches_server(ctx, name)
    matches: list[SourceMatch] = []
    for source_id, source_path, conn in all_project_connections(ctx):
        for entity in entities_repo.search(conn, name):
            matches.append(SourceMatch(source_id=source_id, source_path=source_path, entity=entity))
    matches.sort(key=lambda m: (m.entity.qualified_name, m.source_id, m.entity.id))
    return matches


def conn_for_source_path(ctx: AppContext, source_path: str) -> sqlite3.Connection:
    """Local-mode-only: opens the per-project sqlite connection for a
    registered source's own path. Guarded the same way as
    ``all_project_connections`` (this phase's single choke point rule) --
    a server-mode ``SourceMatch.source_path`` is a placeholder (the
    source id, not a real filesystem path; see
    ``source_match_from_symbol_hit``), so silently resolving it into a
    project id and opening a local sqlite file would both be meaningless
    and a silent local fallback. Callers that still reach here in server
    mode (``cli/impact.py``'s/``cli/explore.py``'s cross-link/document
    lookups, which have no backend-contract equivalent yet -- documented
    gap, see those modules) now raise instead of doing that.
    """
    if ctx.config.storage.mode != "local":
        raise LocalStorageModeRequiredError(
            "conn_for_source_path: storage.mode is "
            f"{ctx.config.storage.mode!r}, not 'local' -- local per-project "
            "sqlite must never be opened outside local mode (no silent "
            "local fallback in server mode)"
        )
    project_id = paths.project_id_for_path(Path(source_path))
    return ctx.project_conn(project_id)


def unresolved_symbol_edges(
    conn: sqlite3.Connection,
    symbol_name: str,
    *,
    relationship_types: tuple[RelationshipType, ...] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[TraversalEdge]:
    if relationship_types:
        edges: list[Relationship] = []
        for rel_type in relationship_types:
            edges.extend(
                relationships_repo.incoming_by_symbol(
                    conn, symbol_name, relationship_type=rel_type, limit=limit
                )
            )
    else:
        edges = relationships_repo.incoming_by_symbol(conn, symbol_name, limit=limit)
    edges.sort(key=_sort_key)
    return [TraversalEdge(depth=1, relationship=edge) for edge in edges[:limit]]


def _traverse_symbol_server(
    ctx: AppContext,
    name: str,
    *,
    direction: Direction,
    relationship_types: tuple[RelationshipType, ...] | None,
    max_depth: int,
    limit: int,
) -> tuple[list[SourceMatch], list[TraversalEdge]]:
    """Server-mode counterpart of ``traverse_symbol``: walks
    ``ctx.backend().graph_neighbors()`` from each matched entity instead of
    the local BFS over ``relationships`` -- never local SQLite, per this
    phase's "no silent local fallback" rule.

    ``graph_neighbors`` itself performs real iterative multi-hop
    expansion up to ``max_depth`` (frontier BFS with cycle protection via
    a visited-entity-id set) and tags each newly discovered edge with its
    actual hop distance from the root in ``hit.payload["_hop_depth"]`` --
    read back here via ``_hop_depth_of`` -- instead of flattening every
    result to depth=1.

    The unresolved-name merge (see this function's own docstring below)
    is still limited to entity-id-reachable edges: a relationship whose
    ``target_entity_id`` is ``None`` is still returned by
    ``graph_neighbors`` when it is reachable from a *resolved* entity id
    (exactly the majority case, and the one the docstring below is about);
    one recorded purely under a bare ``target_symbol`` with no resolved
    ``source_entity_id``/``target_entity_id`` anywhere in the frontier at
    all is merged in separately via ``find_unresolved_relationships``
    (see ``retrieval/graph.py``'s ``_resolved_server`` for the model this
    follows) -- callers/``ragmonk callers`` route through
    ``retrieval.graph.resolved_incoming`` for that; this lower-level
    ``traverse_symbol`` entry point intentionally keeps the same
    entity-id-reachable scope it always had.
    """
    matches = find_symbol_matches(ctx, name)
    if not matches:
        return matches, []
    backend = ctx.backend()
    backend_direction = _DIRECTION_TO_BACKEND[direction]
    filters: dict[str, Any] | None = None
    if relationship_types:
        filters = {"relationship_type": [rt.value for rt in relationship_types]}
    edges: list[TraversalEdge] = []
    seen_ids: set[str] = set()
    for match in matches:
        hits = backend.graph_neighbors(match.entity.id, backend_direction, max_depth, filters)
        for hit in hits:
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)
            relationship = relationship_from_search_hit(hit)
            edges.append(TraversalEdge(depth=_hop_depth_of(hit), relationship=relationship))
    edges.sort(key=lambda e: (e.depth, *_sort_key(e.relationship)))
    return matches, edges[:limit]


def _hop_depth_of(hit: SearchHit) -> int:
    """The real hop distance ``graph_neighbors`` tagged this hit with
    (``hit.payload["_hop_depth"]``), falling back to 1 for any hit that
    predates that field or came from a synthesized/unresolved payload
    (e.g. ``find_unresolved_relationships`` results, which have no
    frontier position of their own and are always direct, depth-1 edges
    off the entity that names them).
    """
    try:
        return int(hit.payload.get("_hop_depth") or 1)
    except (TypeError, ValueError):
        return 1


def traverse_symbol(
    ctx: AppContext,
    name: str,
    *,
    direction: Direction,
    relationship_types: tuple[RelationshipType, ...] | None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[SourceMatch], list[TraversalEdge]]:
    """Resolves ``name`` to entities and walks the graph from each match.

    ``direction="incoming"`` also merges in unresolved (name-only) edges
    -- not just when ``name`` matches no entity at all, but *even when it
    does*: a caller processed earlier in the same indexing run than the
    file defining ``name`` (scan order is not guaranteed -- see
    ``sources/scanner.py``) has its call resolved by ``code/resolver.py``
    only down to ``target_symbol``, never retroactively upgraded to
    ``target_entity_id`` once the definition is indexed. Without this
    merge such a caller is silently invisible to ``callers``/``impact``
    despite being a real, evidenced edge.

    The unresolved row's ``target_symbol`` is whatever ``resolve_reference``
    fell back to, which is the *bare* name, not necessarily the literal
    ``name`` queried here -- e.g. querying the qualified
    ``pkg.Class.method`` must still find a row recorded as bare
    ``"method"``. So this searches unresolved edges under ``name`` itself
    AND every matched entity's bare name, not ``name`` alone.

    ``outgoing`` has no such fallback: with no resolved entity there is
    nothing to walk callees *from*.
    """
    if ctx.config.storage.mode == "server":
        return _traverse_symbol_server(
            ctx, name, direction=direction, relationship_types=relationship_types,
            max_depth=max_depth, limit=limit,
        )
    matches = find_symbol_matches(ctx, name)
    edges: list[TraversalEdge] = []
    if matches:
        seen: set[tuple[int, str]] = set()
        for match in matches:
            conn = conn_for_source_path(ctx, match.source_path)
            edges.extend(
                traverse(
                    conn,
                    match.entity.id,
                    direction=direction,
                    relationship_types=relationship_types,
                    max_depth=max_depth,
                    limit=limit,
                )
            )
            if direction != "incoming":
                continue
            for symbol in {name, match.entity.name}:
                key = (id(conn), symbol)
                if key in seen:
                    continue
                seen.add(key)
                edges.extend(
                    unresolved_symbol_edges(
                        conn, symbol, relationship_types=relationship_types, limit=limit
                    )
                )
        edges.sort(key=lambda e: (e.depth, *_sort_key(e.relationship)))
        return matches, edges[:limit]

    if direction == "incoming":
        for _source_id, _source_path, conn in all_project_connections(ctx):
            edges.extend(
                unresolved_symbol_edges(
                    conn, name, relationship_types=relationship_types, limit=limit
                )
            )
        edges.sort(key=lambda e: _sort_key(e.relationship))
        return matches, edges[:limit]

    return matches, edges
