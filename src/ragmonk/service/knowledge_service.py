"""Code-knowledge queries (symbols, callers, callees, references, impact).

Admin UI plan, Phase 5 (§8.3): expose the code-knowledge graph in the
browser. Reuses ``code.graph.traverse_symbol`` and ``cli.impact._run`` --
the same traversal the ``ragmonk callers|callees|references|impact``
commands use.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from ragmonk.cli._code_common import edge_to_dict, match_to_dict
from ragmonk.code.graph import DEFAULT_LIMIT, DEFAULT_MAX_DEPTH, traverse_symbol
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import RelationshipType
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import entities_repo


def list_symbols(
    ctx: AppContext, *, query: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    rows: list[dict[str, Any]] = []
    if ctx.config.storage.mode == "server":
        # Completion plan F3: symbols come from the server backend
        # (published generation only), never local SQLite.
        for entity in ctx.backend().list_entities(query=query or None, limit=limit):
            rows.append(
                {
                    "id": entity.id,
                    "name": entity.name,
                    "qualified_name": entity.qualified_name,
                    "kind": entity.kind.value,
                    "language": entity.language,
                    "source_id": entity.source_id,
                    "start_line": entity.start_line,
                }
            )
        rows.sort(key=lambda r: r["qualified_name"])
        return rows[:limit]
    for source in registry.list():
        # Independent review BLOCKER fix: genuine knowledge-data read (the
        # symbol list itself) -- deliberately left as the default
        # control_plane=False so this raises in server mode instead of
        # silently reading local sqlite.
        conn = ctx.project_conn(paths.project_id_for_path(Path(source.path)))
        if query:
            try:
                entities = entities_repo.search_fts(conn, query, limit=limit)
            except sqlite3.OperationalError:
                # A raw FTS5 MATCH can reject punctuation/operator syntax a
                # user might type into the search box -- treat that as "no
                # matches" rather than surfacing a 500.
                entities = []
        else:
            entities = entities_repo.list_all(conn)
        for entity in entities:
            rows.append(
                {
                    "id": entity.id,
                    "name": entity.name,
                    "qualified_name": entity.qualified_name,
                    "kind": entity.kind.value,
                    "language": entity.language,
                    "source_id": source.id,
                    "start_line": entity.start_line,
                }
            )
            if len(rows) >= limit:
                break
    rows.sort(key=lambda r: r["qualified_name"])
    return rows[:limit]


def _traverse(
    ctx: AppContext,
    name: str,
    *,
    direction: Literal["incoming", "outgoing"],
    rel_types: tuple[RelationshipType, ...],
    max_depth: int,
    limit: int,
) -> dict[str, Any]:
    matches, edges = traverse_symbol(
        ctx,
        name,
        direction=direction,
        relationship_types=rel_types,
        max_depth=max_depth,
        limit=limit,
    )
    return {
        "query": name,
        "matches": [match_to_dict(m) for m in matches],
        "edges": [edge_to_dict(e) for e in edges],
    }


def callers(
    ctx: AppContext, name: str, *, max_depth: int = DEFAULT_MAX_DEPTH, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    return _traverse(
        ctx, name, direction="incoming", rel_types=(RelationshipType.CALLS,),
        max_depth=max_depth, limit=limit,
    )


def callees(
    ctx: AppContext, name: str, *, max_depth: int = DEFAULT_MAX_DEPTH, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    return _traverse(
        ctx, name, direction="outgoing", rel_types=(RelationshipType.CALLS,),
        max_depth=max_depth, limit=limit,
    )


def references(
    ctx: AppContext, name: str, *, max_depth: int = DEFAULT_MAX_DEPTH, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    return _traverse(
        ctx, name, direction="incoming", rel_types=(RelationshipType.REFERENCES,),
        max_depth=max_depth, limit=limit,
    )


def impact(
    ctx: AppContext, name: str, *, max_depth: int = DEFAULT_MAX_DEPTH, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    from ragmonk.cli.impact import _run as impact_run  # noqa: PLC0415 - reuse CLI aggregation

    return impact_run(ctx, name, max_depth=max_depth, limit=limit)
