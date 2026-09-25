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
from typing import Any

from ragmonk.backends.base import GraphDirection
from ragmonk.backends.models import FileRecord as BackendFileRecord
from ragmonk.code.graph import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_DEPTH,
    SourceMatch,
    TraversalEdge,
    conn_for_source_path,
    find_symbol_matches,
    relationship_from_search_hit,
    traverse,
    unresolved_symbol_edges,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Entity, FileKind, FileRecord, FileStatus, RelationshipType
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


def _references_server(
    ctx: AppContext, name: str, *, max_depth: int, limit: int
) -> tuple[list[SourceMatch], list[TraversalEdge]]:
    """Server-mode counterpart of ``references``: walks
    ``ctx.backend().graph_neighbors()`` both directions from each matched
    entity instead of the local BFS -- never local SQLite. Scope cut
    (documented, not forced): the unresolved (name-only) edge merge has no
    server-mode equivalent -- see ``code/graph.py``'s
    ``_traverse_symbol_server`` docstring for exactly why.
    """
    matches = find_symbol_matches(ctx, name)
    if not matches:
        return matches, []
    backend = ctx.backend()
    filters: dict[str, Any] = {"relationship_type": [rt.value for rt in REFERENCE_TYPES]}
    edges: list[TraversalEdge] = []
    seen_ids: set[str] = set()
    for match in matches:
        for backend_direction in ("in", "out"):
            hits = backend.graph_neighbors(match.entity.id, backend_direction, max_depth, filters)
            for hit in hits:
                if hit.id in seen_ids:
                    continue
                seen_ids.add(hit.id)
                edges.append(TraversalEdge(depth=1, relationship=relationship_from_search_hit(hit)))
    edges.sort(
        key=lambda e: (
            e.depth,
            e.relationship.relationship_type.value,
            e.relationship.target_entity_id or e.relationship.target_symbol or "",
            e.relationship.id,
        )
    )
    return matches, edges[:limit]


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
    if ctx.config.storage.mode == "server":
        return _references_server(ctx, name, max_depth=max_depth, limit=limit)
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


def _resolved_server(
    ctx: AppContext,
    matches: list[SourceMatch],
    *,
    backend_direction: GraphDirection,
    relationship_types: tuple[RelationshipType, ...],
    max_depth: int,
    limit: int,
) -> list[ResolvedEdge]:
    """Shared server-mode body for ``resolved_incoming``/``resolved_outgoing``.

    Completion plan F4: each ``graph_neighbors`` edge's *other* end is
    resolved to a real ``Entity``/``FileRecord`` through the backend's
    targeted read primitives (``get_entities``/``get_files``, both
    batched and filtered to the published generation) -- parity with
    local mode's ``entities_repo.get``/``files_repo.get``. Never local
    SQLite. An edge whose neighbor id is genuinely unresolved (name-only
    ``target_symbol``) still carries ``None``, exactly like local mode.
    """
    backend = ctx.backend()
    filters: dict[str, Any] = {"relationship_type": [rt.value for rt in relationship_types]}
    raw: list[tuple[str, TraversalEdge]] = []
    seen_ids: set[str] = set()
    for match in matches:
        hits = backend.graph_neighbors(match.entity.id, backend_direction, max_depth, filters)
        for hit in hits:
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)
            relationship = relationship_from_search_hit(hit)
            raw.append((match.source_id, TraversalEdge(depth=1, relationship=relationship)))
    raw = raw[:limit]
    direction = "incoming" if backend_direction == "in" else "outgoing"
    neighbor_ids = [
        nid for _, edge in raw if (nid := _neighbor_id(edge, direction)) is not None
    ]
    entities = {e.id: e for e in backend.get_entities(neighbor_ids)} if neighbor_ids else {}
    file_ids = sorted({e.file_id for e in entities.values()})
    files = {f.file_id: f for f in backend.get_files(file_ids)} if file_ids else {}
    out: list[ResolvedEdge] = []
    for source_id, edge in raw:
        nid = _neighbor_id(edge, direction)
        entity = entities.get(nid) if nid else None
        backend_file = files.get(entity.file_id) if entity is not None else None
        out.append(
            ResolvedEdge(
                edge=edge,
                source_id=source_id,
                neighbor_entity=entity,
                neighbor_file=_core_file_record(backend_file) if backend_file else None,
            )
        )
    return out


def _neighbor_id(edge: TraversalEdge, direction: str) -> str | None:
    rel = edge.relationship
    return rel.source_entity_id if direction == "incoming" else rel.target_entity_id


def _core_file_record(record: BackendFileRecord) -> FileRecord:
    """Backend-neutral ``FileRecord`` -> the core model impact/explore
    render (path/size/status). Fields the server doesn't store fall back
    to neutral defaults.
    """
    meta = record.metadata or {}
    kind = meta.get("kind") or FileKind.CODE.value
    status = meta.get("status") or FileStatus.INDEXED.value
    updated = str(meta.get("updated_at") or "")
    return FileRecord(
        id=record.file_id,
        source_id=record.source_id,
        path=record.path,
        kind=FileKind(kind),
        size=record.size_bytes,
        mtime=float(record.mtime or 0.0),
        content_hash=record.content_hash or None,
        status=FileStatus(status),
        last_indexed_at=meta.get("last_indexed_at"),
        created_at=updated,
        updated_at=updated,
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
    if ctx.config.storage.mode == "server":
        return _resolved_server(
            ctx, matches, backend_direction="in", relationship_types=relationship_types,
            max_depth=max_depth, limit=limit,
        )
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
    if ctx.config.storage.mode == "server":
        return _resolved_server(
            ctx, matches, backend_direction="out", relationship_types=relationship_types,
            max_depth=max_depth, limit=limit,
        )
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

    In server mode ``resolved_incoming`` resolves each caller's real
    file through the backend (completion plan F4), so this works the
    same way in both modes.
    """
    incoming = resolved_incoming(
        ctx, matches, name, relationship_types=REFERENCE_TYPES, max_depth=max_depth, limit=limit
    )
    return [
        edge
        for edge in incoming
        if edge.neighbor_file is not None and is_test_file(edge.neighbor_file.path)
    ]
