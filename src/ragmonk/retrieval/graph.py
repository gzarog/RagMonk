"""Retrieval-layer composition over Phase 2's ``code/graph.py`` BFS.

Nothing here reimplements graph walking -- ``code/graph.py``'s ``traverse``/
``traverse_symbol``/``find_symbol_matches`` already give a depth- and
result-capped, deterministically ordered BFS, exactly what the blueprint
asks Phase 5's ``impact`` to reuse rather than growing its own. This
module adds the two things Phase 2's CLI commands didn't need:

1. ``references`` -- the incoming+outgoing CALLS/IMPORTS/REFERENCES merge
   ``cli/references.py`` used to inline. Extracted here so
   ``cli/references.py``, ``cli/impact.py`` and ``cli/explore.py`` share
   one implementation instead of three copies of the same aggregation.
2. Edge *resolution* (``resolved_incoming``/``resolved_outgoing``) --
   ``impact``/``explore`` need each edge's neighboring entity and file,
   not just the raw ``Relationship`` row, to report caller/callee/test
   names and locations.
3. ``is_test_file``/``find_tests_referencing`` -- the small, documented
   naming-convention heuristic behind the "tests" signal in both
   ``impact`` and ``explore``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ragmonk.code.graph import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_DEPTH,
    SourceMatch,
    TraversalEdge,
    conn_for_source_path,
    find_symbol_matches,
    traverse,
    unresolved_symbol_edges,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Entity, FileRecord, RelationshipType
from ragmonk.storage.repositories import entities_repo, files_repo

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_DEPTH",
    "REFERENCE_TYPES",
    "ResolvedEdge",
    "SourceMatch",
    "TraversalEdge",
    "conn_for_source_path",
    "find_symbol_matches",
    "find_tests_referencing",
    "is_test_file",
    "references",
    "resolved_incoming",
    "resolved_outgoing",
]

REFERENCE_TYPES = (RelationshipType.CALLS, RelationshipType.IMPORTS, RelationshipType.REFERENCES)


def references(
    ctx: AppContext,
    name: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[SourceMatch], list[TraversalEdge]]:
    """All CALLS/IMPORTS/REFERENCES edges touching ``name``, either
    direction -- see module docstring point 1. Incoming also merges in
    unresolved (name-only) edges against ``name`` AND each match's bare
    entity name (an unresolved row is stored under whichever bare name
    ``code/resolver.py`` fell back to, not necessarily the literal query
    text -- see ``code/graph.py``'s ``traverse_symbol`` docstring), same
    rationale as ``resolved_incoming``.
    """
    matches = find_symbol_matches(ctx, name)
    edges: list[TraversalEdge] = []
    seen: set[tuple[int, str]] = set()
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        edges.extend(
            traverse(
                conn,
                match.entity.id,
                direction="incoming",
                relationship_types=REFERENCE_TYPES,
                max_depth=max_depth,
                limit=limit,
            )
        )
        for symbol in {name, match.entity.name}:
            key = (id(conn), symbol)
            if key in seen:
                continue
            seen.add(key)
            edges.extend(
                unresolved_symbol_edges(
                    conn, symbol, relationship_types=REFERENCE_TYPES, limit=limit
                )
            )
        edges.extend(
            traverse(
                conn,
                match.entity.id,
                direction="outgoing",
                relationship_types=REFERENCE_TYPES,
                max_depth=max_depth,
                limit=limit,
            )
        )
    edges.sort(
        key=lambda e: (
            e.depth,
            e.relationship.relationship_type.value,
            e.relationship.target_entity_id or e.relationship.target_symbol or "",
            e.relationship.id,
        )
    )
    return matches, edges[:limit]


@dataclass(frozen=True)
class ResolvedEdge:
    """One traversal edge with its *other* end (relative to the symbol
    being explored) resolved to an ``Entity``/``FileRecord`` where
    possible -- ``None`` when the edge only names an unresolved symbol
    (see ``code/graph.py``'s ``unresolved_symbol_edges``).
    """

    edge: TraversalEdge
    source_id: str
    neighbor_entity: Entity | None
    neighbor_file: FileRecord | None


def _resolve(
    conn: sqlite3.Connection, source_id: str, edge: TraversalEdge, direction: str
) -> ResolvedEdge:
    rel = edge.relationship
    neighbor_id = rel.source_entity_id if direction == "incoming" else rel.target_entity_id
    neighbor_entity = entities_repo.get(conn, neighbor_id) if neighbor_id else None
    neighbor_file = (
        files_repo.get(conn, neighbor_entity.file_id) if neighbor_entity is not None else None
    )
    return ResolvedEdge(
        edge=edge, source_id=source_id, neighbor_entity=neighbor_entity, neighbor_file=neighbor_file
    )


def resolved_incoming(
    ctx: AppContext,
    matches: list[SourceMatch],
    name: str,
    *,
    relationship_types: tuple[RelationshipType, ...],
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> list[ResolvedEdge]:
    """Incoming edges of ``relationship_types`` into every match, each
    with its source (caller) entity/file resolved.

    Also merges in unresolved (name-only) edges recorded against ``name``
    AND each match's bare entity name -- see ``code/graph.py``'s
    ``traverse_symbol`` docstring for why a resolved match does not make
    these redundant, and why the bare name is searched too (an unresolved
    row is stored under whatever bare name ``code/resolver.py`` fell back
    to, which may differ from the literal ``name`` queried here, e.g. a
    qualified query still needs to find a bare-name-stored row): a caller
    indexed before ``name``'s defining file existed has its call recorded
    by ``target_symbol`` alone, never retroactively upgraded, and would
    otherwise be silently missing from ``impact``/``explore``'s callers.
    Their resolved *source* (caller) entity is still exactly known --
    it's only the target end that was unresolved at write time.
    """
    out: list[ResolvedEdge] = []
    seen: set[tuple[int, str]] = set()
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        edges = traverse(
            conn,
            match.entity.id,
            direction="incoming",
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )
        out.extend(_resolve(conn, match.source_id, e, "incoming") for e in edges)
        for symbol in {name, match.entity.name}:
            key = (id(conn), symbol)
            if key in seen:
                continue
            seen.add(key)
            unresolved = unresolved_symbol_edges(
                conn, symbol, relationship_types=relationship_types, limit=limit
            )
            out.extend(_resolve(conn, match.source_id, e, "incoming") for e in unresolved)
    return out


def resolved_outgoing(
    ctx: AppContext,
    matches: list[SourceMatch],
    *,
    relationship_types: tuple[RelationshipType, ...],
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> list[ResolvedEdge]:
    """Outgoing edges of ``relationship_types`` from every match, each
    with its target (callee) entity/file resolved.
    """
    out: list[ResolvedEdge] = []
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        edges = traverse(
            conn,
            match.entity.id,
            direction="outgoing",
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )
        out.extend(_resolve(conn, match.source_id, e, "outgoing") for e in edges)
    return out


# A small, documented set of common per-language test-file naming
# conventions -- not a test-framework-detection subsystem. Scoped to the
# languages Phase 2 actually extracts entities for (Python/JS/TS/Go/Java/
# Rust/C#); a project using an unlisted convention (or a language Phase 2
# doesn't parse) just gets no tests signal, the same graceful-miss
# behavior the rest of Phase 5's heuristics have rather than a false one.
_TEST_FILE_PATTERNS = (
    re.compile(r"(?:^|/)test_[^/]+\.py$"),
    re.compile(r"(?:^|/)[^/]+_test\.py$"),
    re.compile(r"(?:^|/)[^/]+_test\.go$"),
    re.compile(r"(?:^|/)[^/]+Tests?\.cs$"),
    re.compile(r"(?:^|/)[^/]+Tests?\.java$"),
    re.compile(r"(?:^|/)[^/]+\.test\.[jt]sx?$"),
    re.compile(r"(?:^|/)[^/]+\.spec\.[jt]sx?$"),
)


def is_test_file(path: str) -> bool:
    # Patterns are written with "/" as the segment separator; stored file
    # paths are platform-native (sources/scanner.py uses str(Path)), so on
    # Windows they'd otherwise never match a "(?:^|/)"-anchored pattern.
    return any(pattern.search(path.replace("\\", "/")) for pattern in _TEST_FILE_PATTERNS)


def find_tests_referencing(
    ctx: AppContext,
    matches: list[SourceMatch],
    name: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    limit: int = DEFAULT_LIMIT,
) -> list[ResolvedEdge]:
    """Entities in test-named files that CALLS/IMPORTS/REFERENCES one of
    ``matches``, reusing Phase 2's already-stored relationship data --
    this filters existing edges by the calling file's name, it does not
    detect test frameworks or run anything. The result's provenance is a
    naming-convention guess (HEURISTIC in spirit); it says nothing about
    ``edge.relationship.confidence``, which keeps meaning "how sure are
    we this call/reference itself is real".
    """
    incoming = resolved_incoming(
        ctx, matches, name, relationship_types=REFERENCE_TYPES, max_depth=max_depth, limit=limit
    )
    return [
        edge
        for edge in incoming
        if edge.neighbor_file is not None and is_test_file(edge.neighbor_file.path)
    ]
