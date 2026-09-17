"""``ragpilot search QUERY [--limit N] [--json] [--snippets] [--table] [--explain]``."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ragpilot.code.graph import all_project_connections
from ragpilot.core.config import SearchConfig
from ragpilot.core.lifecycle import AppContext
from ragpilot.retrieval import (
    context_builder,
    lexical,
    merger,
    neural_reranker,
    query_classifier,
    reranker,
    semantic,
)
from ragpilot.retrieval.context_builder import ExpandedChunkContext
from ragpilot.retrieval.lexical import SearchResult

from ._common import cli_command, console, print_json

_HITS_TABLE_COLUMNS = ("Kind", "Tier", "Title", "Path", "Source")


def _hits_table(results: list[SearchResult]) -> Table:
    table = Table(*_HITS_TABLE_COLUMNS)
    for result in results:
        table.add_row(
            result.kind, result.tier.name.lower(), result.title, result.path, result.source_id
        )
    return table


def _format_label(path: str) -> str:
    suffix = Path(path).suffix.lstrip(".").upper()
    return suffix or "FILE"


def _print_expanded_context(expanded: ExpandedChunkContext) -> None:
    """Renders a matched chunk's expanded context (Search Quality
    Improvement Plan, Phase 9) as its own labelled block, underneath the
    ``Match:`` block -- kept structurally separate (its own heading,
    ``[heading]``/``[previous]``/``[next]`` tags per piece) so it never
    reads as more matched text, only as context around it.
    """
    if not (expanded.parent_heading or expanded.previous or expanded.next):
        return
    console.print("Expanded context:")
    if expanded.parent_heading:
        console.print(f"[heading] {expanded.parent_heading.text}")
    for piece in expanded.previous:
        console.print(f"[previous] {piece.text}")
    for piece in expanded.next:
        console.print(f"[next] {piece.text}")
    console.print("")


def _print_document_snippet(
    result: SearchResult,
    *,
    fallback_modes: list[str],
    expanded: ExpandedChunkContext | None = None,
) -> None:
    """Renders one document hit as a match-centered block:

    ```
    PDF: 1177646_0076000_1.pdf
    Page: 2
    Match:
    HDL Cholesterol .......... 51 mg/dL
    ```

    A hit with no real match snippet (``result.snippet`` falsy -- rare
    for an FTS-sourced hit, see ``documents_repo.search_fts_projection``,
    but possible for an exact-title hit with no FTS row at all) degrades
    to whichever mode comes next in ``fallback_modes`` -- a compact JSON
    dump of the hit for "json", or just its path for "files"/anything
    else -- rather than printing a block with an empty ``Match:``.
    """
    location = result.location or {}
    if result.snippet:
        console.print(f"[bold]{_format_label(result.path)}:[/bold] {result.path}")
        page_start = location.get("page_start")
        page_end = location.get("page_end")
        if page_start is not None:
            page_label = (
                str(page_start)
                if page_end in (None, page_start)
                else f"{page_start}-{page_end}"
            )
            console.print(f"Page: {page_label}")
        elif location.get("heading_path"):
            console.print(f"Section: {' > '.join(location['heading_path'])}")
        elif location.get("section"):
            console.print(f"Section: {location['section']}")
        console.print("Match:")
        console.print(result.snippet)
        if expanded is not None:
            _print_expanded_context(expanded)
        console.print("")
        return

    next_mode = next((mode for mode in fallback_modes if mode in ("json", "files")), "files")
    if next_mode == "json":
        console.print_json(data=result.to_dict())
    else:
        console.print(result.path)


def _print_snippets_mode(
    results: list[SearchResult],
    *,
    fallback: list[str],
    expanded_context: dict[str, ExpandedChunkContext],
) -> None:
    other_hits = [r for r in results if r.kind != "document"]
    doc_hits = [r for r in results if r.kind == "document"]
    if other_hits:
        console.print(_hits_table(other_hits))
    fallback_after_snippets = [mode for mode in fallback if mode != "snippets"]
    for result in doc_hits:
        _print_document_snippet(
            result,
            fallback_modes=fallback_after_snippets,
            expanded=expanded_context.get(result.id),
        )


def _expand_document_contexts(
    ctx: AppContext, results: list[SearchResult], search_config: SearchConfig
) -> dict[str, ExpandedChunkContext]:
    """Search Quality Improvement Plan, Phase 9: expands every document-
    kind hit's matched chunk with its parent heading/sibling chunks,
    strictly after ``results`` is already ranked and sliced to
    ``--limit`` -- this never touches ranking, only what gets shown
    alongside an already-selected hit.

    Skipped entirely (no connections opened, no lookups run) when
    ``search.context`` is fully off (``parent_heading`` false and both
    sibling counts zero) or there are no document hits, so a caller that
    never wants this pays nothing extra for it -- see
    ``SearchContextConfig``'s docstring.
    """
    context_cfg = search_config.context
    if not (context_cfg.parent_heading or context_cfg.previous_chunks or context_cfg.next_chunks):
        return {}
    doc_hits = [r for r in results if r.kind == "document"]
    if not doc_hits:
        return {}

    conns: dict[str, sqlite3.Connection] = {
        source_id: conn for source_id, _source_path, conn in all_project_connections(ctx)
    }
    expanded: dict[str, ExpandedChunkContext] = {}
    for result in doc_hits:
        conn = conns.get(result.source_id)
        if conn is None:
            continue
        piece = context_builder.expand_chunk_context(conn, result.id, config=context_cfg)
        if piece is not None:
            expanded[result.id] = piece
    return expanded


def _print_files_mode(results: list[SearchResult]) -> None:
    other_hits = [r for r in results if r.kind != "document"]
    doc_hits = [r for r in results if r.kind == "document"]
    if other_hits:
        console.print(_hits_table(other_hits))
    seen: set[str] = set()
    for result in doc_hits:
        if result.path not in seen:
            seen.add(result.path)
            console.print(result.path)


@cli_command
def search(
    query: Annotated[
        str, typer.Argument(help="Identifier, phrase, or path fragment to search for.")
    ],
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = lexical.DEFAULT_LIMIT,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    snippets: Annotated[
        bool,
        typer.Option(
            "--snippets",
            help="Show document hits as match-centered snippet blocks (the default; "
            "see search.output.fallback in config.yaml).",
        ),
    ] = False,
    table_output: Annotated[
        bool, typer.Option("--table", help="Show every hit as a plain title/path/tier table.")
    ] = False,
    explain: Annotated[
        bool, typer.Option("--explain", help="Show per-stage timing diagnostics.")
    ] = False,
    hybrid: Annotated[
        bool,
        typer.Option(
            "--hybrid",
            help="Also show one merged, reranked view of lexical and semantic results.",
        ),
    ] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        search_config = ctx.config.search
        timed = lexical.search_with_timings(ctx, query, limit=limit)
        results = timed.results
        timings = list(timed.timings)

        # Blueprint section 18: when ``lazy_semantic`` is on, a
        # high-confidence lexical hit (exact/qualified/alias symbol, or
        # an exact title match) skips semantic search entirely --
        # off by default, since ``search.semantic``'s existing contract
        # is "always attach a semantic section when this is on" (see
        # ``SearchConfig.lazy_semantic``'s docstring).
        confidence = query_classifier.estimate_confidence(results)
        run_semantic = search_config.semantic and not (
            search_config.lazy_semantic and confidence == query_classifier.SearchConfidence.HIGH
        )

        # Only called (and only imports torch/transformers, see
        # retrieval/embedder.py) when semantic search actually needs to
        # run -- the default, disabled path behaves exactly like Phase 5
        # left it. Kept as its own section rather than merged into
        # ``results`` above: a similarity score is a distinct signal
        # from lexical rank, never mixed into the same ranked list (see
        # retrieval/semantic.py's docstring).
        semantic_result = None
        if run_semantic:
            started = time.perf_counter()
            semantic_result = semantic.semantic_search(
                ctx,
                query,
                config=search_config,
                limit=limit,
                candidate_k=search_config.semantic_top_k,
            )
            timings.append(
                lexical.StageTiming(
                    name="semantic",
                    hits=len(semantic_result.results),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            )

        # Blueprint sections 21/22: an additive, opt-in merged+reranked
        # view -- never replaces ``results``/``semantic`` above, which
        # keep their own established, separately-tested contracts.
        ranked_hits = None
        if hybrid:
            semantic_hits = list(semantic_result.results) if semantic_result else []
            candidates = merger.merge(results, semantic_hits)
            reranker_config = search_config.reranker

            # Search Quality Improvement Plan, Phase 11 (optional, off by
            # default -- see SearchRerankerConfig's docstring): when
            # enabled, RRF fusion is asked for at least ``top_n`` hits
            # (never fewer than the caller's own ``limit``) so the neural
            # pass has its full configured pool to rescore, then the
            # combined list is sliced back down to ``limit`` afterward.
            # Disabled (the default), this is exactly the pre-Phase-11
            # call -- same arguments, same result, zero added latency.
            if reranker_config.enabled:
                pool_limit = max(limit, reranker_config.top_n)
                ranked_hits = reranker.rerank(candidates, limit=pool_limit)
                started = time.perf_counter()
                ranked_hits = neural_reranker.rerank_hits(
                    query, ranked_hits, top_n=reranker_config.top_n
                )
                timings.append(
                    lexical.StageTiming(
                        name="neural_rerank",
                        hits=min(len(ranked_hits), reranker_config.top_n),
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                )
                ranked_hits = ranked_hits[:limit]
            else:
                ranked_hits = reranker.rerank(candidates, limit=limit)

        # Precedence: an explicit flag always wins over
        # ``search.output.fallback``'s configured default (``fallback[0]``,
        # "snippets" out of the box) -- the same convention ``--json``
        # already had over the plain default before this mode existed.
        # ``--table`` is the escape hatch back to the single unified table
        # every result kind shared before this mode existed.
        if json_output:
            effective_mode = "json"
        elif snippets:
            effective_mode = "snippets"
        elif table_output:
            effective_mode = "table"
        else:
            effective_mode = search_config.output.fallback[0]

        # Search Quality Improvement Plan, Phase 9: only "json"/"snippets"
        # ever render a matched chunk's expanded context, so "table"/
        # "files" mode never pays for the extra per-hit DB lookups --
        # see ``_expand_document_contexts``'s docstring.
        expanded_context: dict[str, ExpandedChunkContext] = {}
        if effective_mode in ("json", "snippets"):
            expanded_context = _expand_document_contexts(ctx, results, search_config)

        if effective_mode == "json":
            result_dicts = []
            for r in results:
                r_dict = r.to_dict()
                expanded = expanded_context.get(r.id)
                if expanded is not None:
                    r_dict["context"] = expanded.to_dict()
                result_dicts.append(r_dict)
            payload: dict[str, object] = {
                "query": query,
                "results": result_dicts,
            }
            if semantic_result is not None:
                payload["semantic"] = {
                    "available": semantic_result.available,
                    "reason": semantic_result.reason,
                    "results": [h.to_dict() for h in semantic_result.results],
                }
            if ranked_hits is not None:
                payload["hybrid"] = [h.to_dict() for h in ranked_hits]
            if explain:
                payload["explain"] = {
                    "query_kind": query_classifier.classify_query(query).value,
                    "lexical_confidence": confidence.value,
                    "semantic_skipped": search_config.semantic and not run_semantic,
                    "stages": [t.to_dict() for t in timings],
                    "total_ms": round(sum(t.duration_ms for t in timings), 3),
                }
            print_json(payload)
            return

        if not results:
            console.print(f"[yellow]No results for '{query}'.[/yellow]")
        elif effective_mode == "table":
            console.print(_hits_table(results))
        elif effective_mode == "files":
            _print_files_mode(results)
        else:
            _print_snippets_mode(
                results, fallback=search_config.output.fallback, expanded_context=expanded_context
            )

        if semantic_result is not None:
            if semantic_result.results:
                console.print("[bold]Semantic matches[/bold]")
                sem_table = Table("Kind", "Score", "Title", "Path", "Source")
                for hit in semantic_result.results:
                    sem_table.add_row(
                        hit.kind, f"{hit.score:.3f}", hit.title, hit.path, hit.source_id
                    )
                console.print(sem_table)
            elif not semantic_result.available:
                console.print(f"[dim]Semantic search unavailable: {semantic_result.reason}[/dim]")
        elif search_config.semantic:
            console.print(
                f"[dim]Semantic search skipped: lexical confidence is {confidence.value}.[/dim]"
            )

        if ranked_hits is not None:
            console.print("[bold]Hybrid ranked results[/bold]")
            hybrid_table = Table("Kind", "Tier", "Title", "Path", "Semantic")
            for ranked_hit in ranked_hits:
                score = ranked_hit.candidate.semantic_score
                hybrid_table.add_row(
                    ranked_hit.candidate.kind,
                    ranked_hit.tier_label,
                    ranked_hit.candidate.title,
                    ranked_hit.candidate.path,
                    f"{score:.3f}" if score is not None else "-",
                )
            console.print(hybrid_table)

        if explain:
            console.print(
                f"[bold]Query kind:[/bold] {query_classifier.classify_query(query).value}  "
                f"[bold]Lexical confidence:[/bold] {confidence.value}"
            )
            explain_table = Table("Stage", "Hits", "Duration (ms)")
            for t in timings:
                explain_table.add_row(t.name, str(t.hits), f"{t.duration_ms:.3f}")
            console.print(explain_table)
            console.print(f"[bold]Total:[/bold] {sum(t.duration_ms for t in timings):.3f} ms")
