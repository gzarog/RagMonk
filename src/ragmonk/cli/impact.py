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
from ragmonk.knowledge import evidence as evidence_mod
from ragmonk.retrieval import graph as retrieval_graph
from ragmonk.storage.repositories import documents_repo, files_repo, links_repo

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
    """Local-mode only: resolves each match's defining file via
    ``conn_for_source_path``/``files_repo`` (local sqlite). Server mode
    has no backend-contract equivalent for "fetch a file by id" either
    (same documented gap as ``retrieval/graph.py``'s ``_resolved_server``)
    -- skipped explicitly here (``cli/search.py``'s Phase 6 precedent for
    a server-mode gap) rather than opening local sqlite or raising and
    losing the rest of ``impact``'s otherwise-working callers/callees/tests.
    """
    if ctx.config.storage.mode == "server":
        return []
    defined: list[dict[str, Any]] = []
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        file = files_repo.get(conn, match.entity.file_id)
        defined.append(
            {
                "entity_id": match.entity.id,
                "qualified_name": match.entity.qualified_name,
                "path": file.path if file is not None else match.entity.file_id,
                "start_line": match.entity.start_line,
                "end_line": match.entity.end_line,
                "source_id": match.source_id,
            }
        )
    return defined


def _documentation(
    ctx: AppContext, matches: list[SourceMatch]
) -> tuple[list[dict[str, Any]], list[Confidence]]:
    """Local-mode only: reads cross-links via ``links_repo``/
    ``documents_repo`` (local sqlite) -- the ``KnowledgeBackend`` contract
    has no read method for cross-links yet (``publish_links`` is write-only,
    see ``backends/base.py``), so this is a genuine gap, not something
    ``symbol_search``/``graph_neighbors`` can express. Skipped explicitly
    in server mode (same rationale as ``_defined_locations``) rather than
    opening local sqlite or raising and losing the rest of ``impact``.
    """
    if ctx.config.storage.mode == "server":
        return [], []
    documents: list[dict[str, Any]] = []
    confidences: list[Confidence] = []
    seen: set[tuple[str, str | None]] = set()
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        for link in links_repo.list_by_entity(conn, match.entity.id):
            key = (link.document_id, link.section_id)
            if key in seen:
                continue
            seen.add(key)
            document = documents_repo.get_document(conn, link.document_id)
            if document is None:
                continue
            file = files_repo.get(conn, document.file_id)
            unit = documents_repo.get_unit(conn, link.section_id) if link.section_id else None
            ev = evidence_mod.from_cross_link(
                link,
                entity=match.entity,
                document_path=file.path if file is not None else document.id,
                unit=unit,
            )
            documents.append(ev.to_dict())
            confidences.append(link.confidence)
    return documents, confidences


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
