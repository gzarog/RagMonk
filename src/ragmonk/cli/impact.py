"""``ragmonk impact SYMBOL [--max-depth N] [--limit N] [--json]``.

Blast-radius bucketing (``_blast_radius``) and the "tests" signal
(``retrieval/graph.py``'s ``find_tests_referencing``) are both clearly-
scoped heuristics, not measured models -- see their docstrings/comments
for exactly what each one does and doesn't claim.

The blueprint's example output names "Produced by"/"Consumed by" --
those map onto ``PRODUCES``/``CONSUMES`` relationship types that
Phases 1-4 never populate (see ``core/models.py``'s ``RelationshipType``
comment: no real signal for them exists in what gets extracted). This
reports what the data model actually has instead: callers (entities that
CALL this symbol) and callees (entities this symbol CALLs) via Phase 2's
resolved CALLS graph.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.code.graph import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_DEPTH,
    SourceMatch,
    conn_for_source_path,
    find_symbol_matches,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, RelationshipType
from ragmonk.knowledge import confidence as confidence_rank
from ragmonk.knowledge.document_links import linked_document_evidence
from ragmonk.retrieval import graph as retrieval_graph
from ragmonk.storage.repositories import files_repo

from ._code_common import no_matches_message
from ._common import cli_command, console, print_json

# Deliberately just three buckets over one count (distinct callers +
# distinct linked documents touched by a change to this symbol), not a
# calibrated/measured model -- a clearly-labelled heuristic: 0-2 touched
# call sites/docs reads as a contained, easily reviewed change; 3-7 as a
# moderate ripple across a handful of consumers; 8+ as broad enough to
# warrant coordinated review before changing the symbol.
_LOW_MAX = 2
_MEDIUM_MAX = 7


def _blast_radius(score: int) -> str:
    if score <= _LOW_MAX:
        return "LOW"
    if score <= _MEDIUM_MAX:
        return "MEDIUM"
    return "HIGH"


def _format_location(payload: dict[str, Any]) -> str:
    path = payload["path"]
    location = payload["location"]
    if location.get("section"):
        return f"{path} §{location['section']}"
    if location.get("page") is not None:
        return f"{path} p{location['page']}"
    return path


def _defined_locations(ctx: AppContext, matches: list[SourceMatch]) -> list[dict[str, Any]]:
    """Each match's defining file. Local mode reads ``files_repo`` via
    ``conn_for_source_path``; server mode (completion plan F4) resolves
    the same file records through ``ctx.backend().get_files`` -- never
    local SQLite.
    """
    paths_by_file: dict[str, str] = {}
    if ctx.config.storage.mode == "server":
        file_ids = sorted({m.entity.file_id for m in matches})
        if file_ids:
            paths_by_file = {f.file_id: f.path for f in ctx.backend().get_files(file_ids)}
    defined: list[dict[str, Any]] = []
    for match in matches:
        if ctx.config.storage.mode == "server":
            path = paths_by_file.get(match.entity.file_id, match.entity.file_id)
        else:
            conn = conn_for_source_path(ctx, match.source_path)
            file = files_repo.get(conn, match.entity.file_id)
            path = file.path if file is not None else match.entity.file_id
        defined.append(
            {
                "entity_id": match.entity.id,
                "qualified_name": match.entity.qualified_name,
                "path": path,
                "start_line": match.entity.start_line,
                "end_line": match.entity.end_line,
                "source_id": match.source_id,
            }
        )
    return defined


def _documentation(
    ctx: AppContext, matches: list[SourceMatch]
) -> tuple[list[dict[str, Any]], list[Confidence]]:
    """Documentation evidence via cross-domain links, in both storage
    modes (completion plan F4 -- see ``knowledge/document_links.py``).
    """
    pairs = linked_document_evidence(ctx, matches)
    return [ev.to_dict() for ev, _ in pairs], [conf for _, conf in pairs]


def _run(ctx: AppContext, name: str, *, max_depth: int, limit: int) -> dict[str, Any]:
    """Shared with Phase 6's ``ragmonk_impact`` MCP tool -- the whole
    blast-radius payload, independent of CLI rendering/JSON printing.
    """
    matches = find_symbol_matches(ctx, name)
    if not matches:
        return {"query": name, "found": False}

    callers = retrieval_graph.resolved_incoming(
        ctx,
        matches,
        name,
        relationship_types=(RelationshipType.CALLS,),
        max_depth=max_depth,
        limit=limit,
    )
    callees = retrieval_graph.resolved_outgoing(
        ctx,
        matches,
        relationship_types=(RelationshipType.CALLS,),
        max_depth=max_depth,
        limit=limit,
    )
    tests = retrieval_graph.find_tests_referencing(
        ctx, matches, name, max_depth=max_depth, limit=limit
    )
    documents, doc_confidences = _documentation(ctx, matches)
    defined = _defined_locations(ctx, matches)

    caller_names = sorted(
        {e.neighbor_entity.qualified_name for e in callers if e.neighbor_entity is not None}
    )
    callee_names = sorted(
        {e.neighbor_entity.qualified_name for e in callees if e.neighbor_entity is not None}
    )
    test_names = sorted(
        {e.neighbor_entity.qualified_name for e in tests if e.neighbor_entity is not None}
    )

    code_confidence = confidence_rank.highest(
        e.edge.relationship.confidence for e in (*callers, *callees)
    )
    document_confidence = confidence_rank.highest(doc_confidences)

    distinct_callers = {e.neighbor_entity.id for e in callers if e.neighbor_entity is not None}
    distinct_documents = {(d["path"], d["location"].get("section")) for d in documents}
    blast_score = len(distinct_callers) + len(distinct_documents)
    blast_radius = _blast_radius(blast_score)

    return {
        "query": name,
        "found": True,
        "defined": defined,
        "callers": caller_names,
        "callees": callee_names,
        "tests": test_names,
        "documentation": documents,
        "confidence": {
            "code_references": code_confidence.value if code_confidence else None,
            "document_links": document_confidence.value if document_confidence else None,
        },
        "blast_radius": blast_radius,
        "blast_radius_score": blast_score,
    }


@cli_command
def impact(
    name: Annotated[str, typer.Argument(help="Symbol name or fully qualified name.")],
    max_depth: Annotated[int, typer.Option("--max-depth", min=1, max=10)] = DEFAULT_MAX_DEPTH,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = DEFAULT_LIMIT,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        payload = _run(ctx, name, max_depth=max_depth, limit=limit)

        if json_output:
            print_json(payload)
            return

        if not payload["found"]:
            console.print(f"[yellow]{no_matches_message(name)}[/yellow]")
            return

        console.print(f"[bold]{name}[/bold]")
        for d in payload["defined"]:
            console.print(f"Defined: {d['path']}:{d['start_line']}")
        console.print(f"Callers: {', '.join(payload['callers']) or '-'}")
        console.print(f"Callees: {', '.join(payload['callees']) or '-'}")
        console.print(f"Tests: {', '.join(payload['tests']) or '-'}")
        doc_strs = [_format_location(d) for d in payload["documentation"]]
        console.print(f"Documentation: {', '.join(doc_strs) or '-'}")
        confidence = payload["confidence"]
        code_conf_str = confidence["code_references"] or "none"
        doc_conf_str = confidence["document_links"] or "none"
        console.print(
            f"Confidence: Code references: {code_conf_str} / Document links: {doc_conf_str}"
        )
        console.print(f"Blast radius: {payload['blast_radius']}")
