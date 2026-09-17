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
from typing import Literal

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Entity, Relationship, RelationshipType
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import entities_repo, relationships_repo

DEFAULT_MAX_DEPTH = 1
DEFAULT_LIMIT = 100

Direction = Literal["incoming", "outgoing"]


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
    """(source_id, source_path, conn) for every registered source."""
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    return [
        (source.id, source.path, ctx.project_conn(paths.project_id_for_path(Path(source.path))))
        for source in registry.list()
    ]


def find_symbol_matches(ctx: AppContext, name: str) -> list[SourceMatch]:
    """Every entity across every registered source matching ``name``
    exactly by bare name or qualified name, deterministically ordered.
    """
    matches: list[SourceMatch] = []
    for source_id, source_path, conn in all_project_connections(ctx):
        for entity in entities_repo.search(conn, name):
            matches.append(SourceMatch(source_id=source_id, source_path=source_path, entity=entity))
    matches.sort(key=lambda m: (m.entity.qualified_name, m.source_id, m.entity.id))
    return matches


def conn_for_source_path(ctx: AppContext, source_path: str) -> sqlite3.Connection:
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
