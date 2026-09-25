"""``ragmonk explore "QUERY" [--json]`` -- the primary retrieval command
(blueprint section 25): runs the query planner's chosen deterministic
strategies (identifier lookup, FTS, graph, document links -- no semantic
search, see ``retrieval/semantic.py``) and assembles one structured
result through the context builder.

"Requirements" and "Incidents" are document *categories* the blueprint
mentions as example entity types (section 17); Phases 1-4 never classify
a document into either, so those sections always come back empty here
rather than this module inventing a classifier to fill them.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.code.graph import SourceMatch, conn_for_source_path, find_symbol_matches
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, RelationshipType
from ragmonk.knowledge import evidence as evidence_mod
from ragmonk.knowledge.evidence import Evidence, EvidenceLocation
from ragmonk.retrieval import context_builder, lexical, planner, semantic
from ragmonk.retrieval import graph as retrieval_graph
from ragmonk.storage.repositories import documents_repo, files_repo, links_repo

from ._common import cli_command, console, print_json

# Display-only confidence *labels* for lexical hits, folded into the same
# Evidence shape as everything else so the context builder can dedupe/
# prioritize/budget all evidence uniformly. These are lexical relevance
# tiers (retrieval/lexical.py's RankTier), not the code/link confidence
# ladder -- the mapping is an approximation for consistent rendering, not
# a new source of truth.
_TIER_CONFIDENCE: dict[lexical.RankTier, Confidence] = {
    lexical.RankTier.EXACT_SYMBOL: Confidence.EXACT,
    lexical.RankTier.QUALIFIED_SYMBOL: Confidence.HIGH,
    lexical.RankTier.ALIAS_SYMBOL: Confidence.MEDIUM,
    lexical.RankTier.TITLE_OR_HEADING: Confidence.HIGH,
    lexical.RankTier.FTS: Confidence.MEDIUM,
    lexical.RankTier.PATH: Confidence.HEURISTIC,
}


def _lexical_evidence(result: lexical.SearchResult) -> context_builder.EvidenceItem:
    loc = result.location or {}
    evidence = Evidence(
        source=f"lexical:{result.kind}",
        path=result.path,
        location=EvidenceLocation(
            line_start=loc.get("line_start"),
            line_end=loc.get("line_end"),
            page=loc.get("page"),
            section=loc.get("section"),
        ),
        entity=result.title,
        relationship="match",
        confidence=_TIER_CONFIDENCE.get(result.tier, Confidence.HEURISTIC),
    )
    return context_builder.EvidenceItem(evidence=evidence, snippet=result.snippet or "")


def _semantic_evidence(hit: semantic.SemanticHit) -> context_builder.EvidenceItem:
    """Always ``Confidence.HEURISTIC`` -- the ladder's lowest tier, never
    EXACT/HIGH/MEDIUM (blueprint sections 19/57: semantic similarity may
    only ever *suggest* evidence, never mint or upgrade a high-confidence
    fact). ``HEURISTIC`` already carries exactly that "not a language
    fact, not an identifier match, a naming-convention-strength guess"
    meaning for ``knowledge/linker.py``'s route heuristic -- a semantic
    similarity hit is weaker evidence still, but reusing the ladder's
    existing floor rather than inventing a lower tier keeps confidence
    comparisons (``knowledge/confidence.py``) meaningful project-wide.
    """
    loc = hit.location or {}
    evidence = Evidence(
        source=f"semantic:{hit.kind}",
        path=hit.path,
        location=EvidenceLocation(
            line_start=loc.get("line_start"),
            line_end=loc.get("line_end"),
            page=loc.get("page"),
            section=loc.get("section"),
        ),
        entity=hit.title,
        relationship="similar_to",
        confidence=Confidence.HEURISTIC,
    )
    return context_builder.EvidenceItem(evidence=evidence, snippet=hit.snippet)


def _edge_evidence(edge: retrieval_graph.ResolvedEdge) -> context_builder.EvidenceItem | None:
    if edge.neighbor_entity is None:
        return None
    file_path = edge.neighbor_file.path if edge.neighbor_file else edge.neighbor_entity.file_id
    evidence = evidence_mod.from_code_relationship(
        edge.edge.relationship, entity=edge.neighbor_entity, file_path=file_path
    )
    snippet = edge.neighbor_entity.signature or edge.neighbor_entity.qualified_name
    return context_builder.EvidenceItem(evidence=evidence, snippet=snippet)


def _graph_path(
    edge: retrieval_graph.ResolvedEdge, *, direction: str, symbol_display: str
) -> context_builder.GraphPath:
    rel = edge.edge.relationship
    other = (
        edge.neighbor_entity.qualified_name
        if edge.neighbor_entity is not None
        else (rel.target_symbol or "?")
    )
    if direction == "incoming":
        return context_builder.GraphPath(
            source=other,
            relationship=rel.relationship_type.value,
            target=symbol_display,
            confidence=rel.confidence.value,
        )
    return context_builder.GraphPath(
        source=symbol_display,
        relationship=rel.relationship_type.value,
        target=other,
        confidence=rel.confidence.value,
    )


def _document_links(
    ctx: AppContext, matches: list[SourceMatch], strategies: tuple[planner.Strategy, ...]
) -> list[Evidence]:
    """Local-mode only: reads cross-links via ``links_repo``/
    ``documents_repo`` (local sqlite) -- see ``cli/impact.py``'s
    ``_documentation`` for why this has no backend-contract equivalent
    yet. Skipped explicitly in server mode rather than opening local
    sqlite or raising and losing the rest of ``explore``'s otherwise-
    working symbols/callers/callees/tests.
    """
    if not matches or planner.Strategy.DOCUMENTS not in strategies:
        return []
    if ctx.config.storage.mode == "server":
        return []
    out: list[Evidence] = []
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
            out.append(
                evidence_mod.from_cross_link(
                    link,
                    entity=match.entity,
                    document_path=file.path if file is not None else document.id,
                    unit=unit,
                )
            )
    return out


def _run(ctx: AppContext, query_plan: planner.QueryPlan) -> dict[str, Any]:
    strategies = query_plan.strategies
    symbol_query = query_plan.symbol or query_plan.query

    lexical_results = (
        lexical.search(ctx, query_plan.query, limit=25)
        if planner.Strategy.FTS in strategies or planner.Strategy.IDENTIFIER in strategies
        else []
    )
    symbol_matches = (
        find_symbol_matches(ctx, symbol_query) if planner.Strategy.IDENTIFIER in strategies else []
    )

    callers: list[retrieval_graph.ResolvedEdge] = []
    callees: list[retrieval_graph.ResolvedEdge] = []
    tests: list[retrieval_graph.ResolvedEdge] = []
    if symbol_matches:
        if planner.Strategy.CALLERS in strategies:
            callers = retrieval_graph.resolved_incoming(
                ctx, symbol_matches, symbol_query, relationship_types=(RelationshipType.CALLS,)
            )
        if planner.Strategy.CALLEES in strategies:
            callees = retrieval_graph.resolved_outgoing(
                ctx, symbol_matches, relationship_types=(RelationshipType.CALLS,)
            )
        if planner.Strategy.TESTS in strategies:
            tests = retrieval_graph.find_tests_referencing(ctx, symbol_matches, symbol_query)

    doc_link_evidence = _document_links(ctx, symbol_matches, strategies)
    symbol_display = symbol_matches[0].entity.qualified_name if symbol_matches else symbol_query

    # Only run (and only imports torch/transformers, see
    # retrieval/embedder.py) when the planner actually added
    # Strategy.SEMANTIC, which itself only happens when search.semantic
    # is on (planner.py) -- the default, disabled path never touches this.
    semantic_result = (
        semantic.semantic_search(ctx, query_plan.query, config=ctx.config.search)
        if planner.Strategy.SEMANTIC in strategies
        else None
    )

    items: list[context_builder.EvidenceItem] = []
    graph_paths: list[context_builder.GraphPath] = []
    for edge in callers:
        item = _edge_evidence(edge)
        if item is not None:
            items.append(item)
        graph_paths.append(_graph_path(edge, direction="incoming", symbol_display=symbol_display))
    for edge in callees:
        item = _edge_evidence(edge)
        if item is not None:
            items.append(item)
        graph_paths.append(_graph_path(edge, direction="outgoing", symbol_display=symbol_display))
    for ev in doc_link_evidence:
        items.append(context_builder.EvidenceItem(evidence=ev, snippet=ev.entity))
    if not symbol_matches:
        items.extend(_lexical_evidence(r) for r in lexical_results if r.snippet)
    if semantic_result is not None:
        items.extend(_semantic_evidence(h) for h in semantic_result.results)

    context_result = context_builder.build_context(items, graph_paths, budget=ctx.config.context)

    documents = [r.to_dict() for r in lexical_results if r.kind == "document"]
    paths = sorted({r.path for r in lexical_results})

    summary = (
        f"Found {len(symbol_matches)} symbol(s) and {len(documents)} document(s) matching "
        f"'{query_plan.query}'; see below for details."
    )

    return {
        "query": query_plan.query,
        "intent": query_plan.intent.value,
        "strategies": [s.value for s in strategies],
        "summary": summary,
        "symbols": [
            {
                "entity_id": m.entity.id,
                "qualified_name": m.entity.qualified_name,
                "kind": m.entity.kind.value,
                "source_id": m.source_id,
            }
            for m in symbol_matches
        ],
        "paths": paths,
        "call_flows": [
            gp.to_dict() for gp in graph_paths if gp.relationship == RelationshipType.CALLS.value
        ],
        "dependencies": [
            {
                "qualified_name": e.neighbor_entity.qualified_name,
                "relationship": e.edge.relationship.relationship_type.value,
            }
            for e in callees
            if e.neighbor_entity is not None
        ],
        "documents": documents,
        "tests": sorted(
            {e.neighbor_entity.qualified_name for e in tests if e.neighbor_entity is not None}
        ),
        "requirements": [],
        "incidents": [],
        "evidence": context_result.evidence,
        "evidence_truncated": context_result.truncated,
        "evidence_truncation_reasons": context_result.truncation_reasons,
        # Surfaced as its own list, distinct from "documents"/"evidence"
        # above, so a caller can tell a similarity-ranked suggestion
        # apart from a lexical/graph match without inspecting each
        # evidence item's "source" string -- also folded into "evidence"
        # (at Confidence.HEURISTIC, see _semantic_evidence) for anything
        # that budgets/renders evidence uniformly.
        "semantic_results": (
            [h.to_dict() for h in semantic_result.results] if semantic_result is not None else []
        ),
        "semantic_available": semantic_result.available if semantic_result is not None else None,
        "semantic_reason": semantic_result.reason if semantic_result is not None else None,
    }


def _print_list(label: str, items: list[str]) -> None:
    console.print(f"[bold]{label}[/bold]: " + (", ".join(items) if items else "-"))


def _render(result: dict[str, Any]) -> None:
    console.print(f"[bold]{result['summary']}[/bold]")
    console.print(f"Intent: {result['intent']}  Strategies: {', '.join(result['strategies'])}")
    _print_list("Relevant symbols", [s["qualified_name"] for s in result["symbols"]])
    _print_list("Relevant paths", result["paths"])
    _print_list(
        "Call flows",
        [f"{p['source']} -[{p['relationship']}]-> {p['target']}" for p in result["call_flows"]],
    )
    _print_list(
        "Dependencies",
        [f"{d['qualified_name']} ({d['relationship']})" for d in result["dependencies"]],
    )
    _print_list("Documents", [d["title"] or d["path"] for d in result["documents"]])
    _print_list("Tests", result["tests"])
    _print_list("Requirements", result["requirements"])
    _print_list("Incidents", result["incidents"])
    if result["semantic_results"]:
        _print_list(
            "Semantic matches",
            [f"{r['title']} ({r['score']:.3f})" for r in result["semantic_results"]],
        )
    elif result["semantic_available"] is False:
        console.print(f"[dim]Semantic search unavailable: {result['semantic_reason']}[/dim]")
    truncated = " (truncated)" if result["evidence_truncated"] else ""
    console.print(f"[bold]Evidence[/bold]: {len(result['evidence'])} item(s){truncated}")


@cli_command
def explore(
    query: Annotated[str, typer.Argument(help="Natural-language or identifier query.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        query_plan = planner.plan(query, semantic_enabled=ctx.config.search.semantic)
        result = _run(ctx, query_plan)
        if json_output:
            print_json(result)
            return
        _render(result)
