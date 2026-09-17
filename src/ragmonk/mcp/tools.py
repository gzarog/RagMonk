"""The 8 ``ragmonk_*`` MCP tools.

Each tool is a thin adapter: validate input -> call the exact same
functions the equivalent CLI command calls (``code/graph.py``,
``retrieval/lexical.py``, ``retrieval/planner.py``, and the CLI modules'
own extracted ``_run`` helpers where one exists) -> shape the result
through a ``schemas.py`` output model. No retrieval/graph/context-builder
logic is reimplemented here.

Every tool call bootstraps its own ``AppContext`` exactly like a CLI
invocation does (same ``RAGMONK_HOME``/config resolution), and the whole
bootstrap-plus-work unit runs inside one ``asyncio.to_thread`` call bounded
by ``asyncio.wait_for(..., timeout=mcp.request_timeout_seconds)``: the
context's sqlite connections are opened *and* used in that same worker
thread (sqlite3 connections are not usable across threads by default), and
if the timeout fires the abandoned thread simply runs to completion and
cleans itself up in the background rather than being force-cancelled --
Python cannot forcibly cancel a running thread, so this wall-clock timeout
is an honest "stop waiting", not true mid-query cancellation.

Errors never cross the MCP boundary as raw exceptions/tracebacks: a
``RagMonkError`` is mapped to a typed ``schemas.ToolError`` by class name
(e.g. ``UsageError``/``SourceUnavailableError``), a timeout to
``type="timeout"``, and any other exception to ``type="internal_error"``
-- always returned as a normal, ``ok=False`` tool result, never a protocol-
level failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ragmonk.cli import ask as ask_cli
from ragmonk.cli import docs as docs_cli
from ragmonk.cli import explore as explore_cli
from ragmonk.cli import impact as impact_cli
from ragmonk.cli import status as status_cli
from ragmonk.cli._code_common import edge_to_dict, match_to_dict
from ragmonk.code.graph import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_DEPTH,
    find_symbol_matches,
    traverse_symbol,
)
from ragmonk.core import paths
from ragmonk.core.config import load_config
from ragmonk.core.errors import RagMonkError, UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import RelationshipType
from ragmonk.retrieval import lexical, planner

from . import schemas
from .schemas import (
    AskInput,
    AskOutput,
    DocumentsInput,
    DocumentsOutput,
    ExploreInput,
    ExploreOutput,
    GraphTraversalInput,
    GraphTraversalOutput,
    ImpactInput,
    ImpactOutput,
    SearchInput,
    SearchOutput,
    StatusInput,
    StatusOutput,
    SymbolInput,
    SymbolOutput,
    ToolError,
)


def _map_error(exc: RagMonkError) -> ToolError:
    return ToolError(type=type(exc).__name__, message=str(exc))


def _require(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise UsageError(f"{field} must not be empty")
    return stripped


async def _call[T: schemas.BaseToolOutput](
    output_cls: type[T], work: Callable[[AppContext], dict[str, Any]]
) -> T:
    try:
        home = paths.ensure_runtime_layout()
        timeout_seconds = load_config(home=home).mcp.request_timeout_seconds
    except RagMonkError as exc:
        return output_cls(ok=False, error=_map_error(exc))

    def _run_all() -> dict[str, Any]:
        # ``log_console_format="json"`` routes WARNING+ console logs
        # through a plain ``logging.StreamHandler`` (stderr by default)
        # instead of the CLI's interactive ``RichHandler`` (stdout by
        # default, see telemetry/logging.py): stdout is the MCP stdio
        # transport's JSON-RPC framing channel, and even one stray log
        # line on it would corrupt the protocol stream for the client.
        with AppContext.bootstrap(log_console_format="json") as ctx:
            return work(ctx)

    try:
        data = await asyncio.wait_for(asyncio.to_thread(_run_all), timeout=timeout_seconds)
    except TimeoutError:
        return output_cls(
            ok=False,
            error=ToolError(
                type="timeout",
                message=f"tool call exceeded mcp.request_timeout_seconds={timeout_seconds}s",
            ),
        )
    except RagMonkError as exc:
        return output_cls(ok=False, error=_map_error(exc))
    except Exception as exc:  # noqa: BLE001 - MCP boundary: never a raw traceback to the client
        return output_cls(ok=False, error=ToolError(type="internal_error", message=str(exc)))
    return output_cls(ok=True, **data)


# -- ragmonk_explore -----------------------------------------------------


def _with_context_overrides(ctx: AppContext, input_: ExploreInput) -> AppContext:
    """Applies ``ExploreInput``'s optional ``max_chars``/``max_files``/
    ``max_graph_nodes`` on top of ``ctx.config.context`` -- the same
    ``ContextConfig`` ``retrieval/context_builder.py`` budgets against --
    without mutating the caller's ``ctx``.
    """
    overrides = {
        field: value
        for field, value in (
            ("max_chars", input_.max_chars),
            ("max_files", input_.max_files),
            ("max_graph_nodes", input_.max_graph_nodes),
        )
        if value is not None
    }
    if not overrides:
        return ctx
    context_config = ctx.config.context.model_copy(update=overrides)
    config = ctx.config.model_copy(update={"context": context_config})
    return replace(ctx, config=config)


def _explore_work(ctx: AppContext, input_: ExploreInput) -> dict[str, Any]:
    query = _require(input_.query, "query")
    ctx = _with_context_overrides(ctx, input_)
    query_plan = planner.plan(query, semantic_enabled=ctx.config.search.semantic)
    result = explore_cli._run(ctx, query_plan)

    warnings: list[str] = []
    if result["evidence_truncated"]:
        warnings.extend(result["evidence_truncation_reasons"])
    if not result["requirements"] and not result["incidents"]:
        warnings.append(
            "Requirements/Incidents are always empty: RagMonk has no document "
            "classifier yet to populate these categories (see cli/explore.py)."
        )

    return {
        "query": result["query"],
        "intent": result["intent"],
        "strategies": result["strategies"],
        "answer_context": result["summary"],
        "entities": result["symbols"],
        "relationships": result["call_flows"],
        "documents": result["documents"],
        "tests": result["tests"],
        "evidence": result["evidence"],
        "warnings": warnings,
    }


async def ragmonk_explore(
    query: str,
    max_chars: int | None = None,
    max_files: int | None = None,
    max_graph_nodes: int | None = None,
) -> ExploreOutput:
    """Primary retrieval tool: runs the same deterministic query planner
    and evidence assembly as ``ragmonk explore "QUERY"``, bounded by the
    context budget (``context.max_chars``/``max_files``/``max_graph_nodes``
    from config, overridable per call).
    """
    input_ = ExploreInput(
        query=query, max_chars=max_chars, max_files=max_files, max_graph_nodes=max_graph_nodes
    )
    return await _call(ExploreOutput, lambda ctx: _explore_work(ctx, input_))


# -- ragmonk_search --------------------------------------------------------


def _search_work(ctx: AppContext, input_: SearchInput) -> dict[str, Any]:
    query = _require(input_.query, "query")
    limit = input_.limit or lexical.DEFAULT_LIMIT
    results = lexical.search(ctx, query, limit=limit)
    return {"query": query, "results": [r.to_dict() for r in results]}


async def ragmonk_search(query: str, limit: int | None = None) -> SearchOutput:
    """Lexical search across code and documents -- same ranking as
    ``ragmonk search QUERY``.
    """
    input_ = SearchInput(query=query, limit=limit)
    return await _call(SearchOutput, lambda ctx: _search_work(ctx, input_))


# -- ragmonk_symbol --------------------------------------------------------


def _symbol_work(ctx: AppContext, input_: SymbolInput) -> dict[str, Any]:
    name = _require(input_.name, "name")
    matches = find_symbol_matches(ctx, name)
    return {"query": name, "matches": [match_to_dict(m) for m in matches]}


async def ragmonk_symbol(name: str) -> SymbolOutput:
    """Look up a code symbol by name or fully qualified name -- same
    resolution as ``ragmonk symbol NAME``.
    """
    input_ = SymbolInput(name=name)
    return await _call(SymbolOutput, lambda ctx: _symbol_work(ctx, input_))


# -- ragmonk_callers / ragmonk_callees ------------------------------------


def _traversal_work(
    ctx: AppContext, input_: GraphTraversalInput, *, direction: str
) -> dict[str, Any]:
    name = _require(input_.name, "name")
    max_depth = input_.max_depth or DEFAULT_MAX_DEPTH
    limit = input_.limit or DEFAULT_LIMIT
    matches, edges = traverse_symbol(
        ctx,
        name,
        direction=direction,  # type: ignore[arg-type]
        relationship_types=(RelationshipType.CALLS,),
        max_depth=max_depth,
        limit=limit,
    )
    return {
        "query": name,
        "matches": [match_to_dict(m) for m in matches],
        "edges": [edge_to_dict(e) for e in edges],
    }


async def ragmonk_callers(
    name: str, max_depth: int | None = None, limit: int | None = None
) -> GraphTraversalOutput:
    """Entities that CALL ``name`` -- same traversal as ``ragmonk callers NAME``."""
    input_ = GraphTraversalInput(name=name, max_depth=max_depth, limit=limit)
    return await _call(
        GraphTraversalOutput, lambda ctx: _traversal_work(ctx, input_, direction="incoming")
    )


async def ragmonk_callees(
    name: str, max_depth: int | None = None, limit: int | None = None
) -> GraphTraversalOutput:
    """Entities that ``name`` CALLS -- same traversal as ``ragmonk callees NAME``."""
    input_ = GraphTraversalInput(name=name, max_depth=max_depth, limit=limit)
    return await _call(
        GraphTraversalOutput, lambda ctx: _traversal_work(ctx, input_, direction="outgoing")
    )


# -- ragmonk_impact ---------------------------------------------------------


def _impact_work(ctx: AppContext, input_: ImpactInput) -> dict[str, Any]:
    name = _require(input_.name, "name")
    max_depth = input_.max_depth or DEFAULT_MAX_DEPTH
    limit = input_.limit or DEFAULT_LIMIT
    return impact_cli._run(ctx, name, max_depth=max_depth, limit=limit)


async def ragmonk_impact(
    name: str, max_depth: int | None = None, limit: int | None = None
) -> ImpactOutput:
    """Blast-radius impact analysis -- same computation as ``ragmonk impact NAME``."""
    input_ = ImpactInput(name=name, max_depth=max_depth, limit=limit)
    return await _call(ImpactOutput, lambda ctx: _impact_work(ctx, input_))


# -- ragmonk_documents -------------------------------------------------------


def _documents_work(ctx: AppContext, input_: DocumentsInput) -> dict[str, Any]:
    source_id = input_.source_id.strip() if input_.source_id else None
    rows = docs_cli._run(ctx, source_id)
    return {"documents": rows}


async def ragmonk_documents(source_id: str | None = None) -> DocumentsOutput:
    """List indexed documents -- same listing as ``ragmonk docs``."""
    input_ = DocumentsInput(source_id=source_id)
    return await _call(DocumentsOutput, lambda ctx: _documents_work(ctx, input_))


# -- ragmonk_status -----------------------------------------------------


def _status_work(ctx: AppContext, _input: StatusInput) -> dict[str, Any]:
    return status_cli._run(ctx)


async def ragmonk_status() -> StatusOutput:
    """Indexing status -- same summary as ``ragmonk status``."""
    return await _call(StatusOutput, lambda ctx: _status_work(ctx, StatusInput()))


# -- ragmonk_ask -------------------------------------------------------


def _ask_work(ctx: AppContext, input_: AskInput) -> dict[str, Any]:
    question = _require(input_.question, "question")
    result = ask_cli._run(ctx, question)
    warnings: list[str] = []
    if result["evidence_truncated"]:
        warnings.extend(result["evidence_truncation_reasons"])
    return {
        "question": result["question"],
        "answer": result["answer"],
        "evidence": result["evidence"],
        "warnings": warnings,
    }


async def ragmonk_ask(question: str) -> AskOutput:
    """Ask a question answered by the configured ``ai:`` provider,
    grounded in the exact same evidence ``ragmonk_explore`` would
    return. Unlike every other tool here, this one is not read-only/
    network-free: it calls out to the configured provider (subject to
    ``privacy.external_ai_allowed`` exactly like ``ragmonk ask``) and
    fails with a typed error (``AiNotConfiguredError``/
    ``AiPrivacyBlockedError``/``AiProviderError``) if no provider is
    configured, the privacy gate blocks it, or the provider call fails.
    """
    input_ = AskInput(question=question)
    return await _call(AskOutput, lambda ctx: _ask_work(ctx, input_))


__all__ = [
    "ragmonk_ask",
    "ragmonk_callees",
    "ragmonk_callers",
    "ragmonk_documents",
    "ragmonk_explore",
    "ragmonk_impact",
    "ragmonk_search",
    "ragmonk_status",
    "ragmonk_symbol",
]
