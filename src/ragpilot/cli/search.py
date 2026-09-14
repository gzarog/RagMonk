"""``ragpilot search QUERY [--limit N] [--json] [--snippets] [--table] [--explain]``."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ragpilot.core.lifecycle import AppContext
from ragpilot.retrieval import lexical, merger, query_classifier, reranker, semantic
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


def _print_document_snippet(result: SearchResult, *, fallback_modes: list[str]) -> None:
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
        console.print("")
        return

    next_mode = next((mode for mode in fallback_modes if mode in ("json", "files")), "files")
    if next_mode == "json":
        console.print_json(data=result.to_dict())
    else:
        console.print(result.path)


def _print_snippets_mode(results: list[SearchResult], *, fallback: list[str]) -> None:
    other_hits = [r for r in results if r.kind != "document"]
    doc_hits = [r for r in results if r.kind == "document"]
    if other_hits:
        console.print(_hits_table(other_hits))
    fallback_after_snippets = [mode for mode in fallback if mode != "snippets"]
    for result in doc_hits:
        _print_document_snippet(result, fallback_modes=fallback_after_snippets)


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

        if effective_mode == "json":
            payload: dict[str, object] = {
                "query": query,
                "results": [r.to_dict() for r in results],
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
            _print_snippets_mode(results, fallback=search_config.output.fallback)

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
