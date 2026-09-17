"""Builds the stdio MCP server (blueprint: "MCP over stdio as the default
agent transport, no external listening port"). ``build_server`` is pure
construction -- no I/O, no blocking loop -- so it is unit-testable on its
own; ``run_stdio`` is the actual blocking entrypoint ``cli/serve.py`` calls.

Uses the high-level ``mcp.server.fastmcp.FastMCP`` API: decorator/registry-
style tool registration with input/output schemas derived from typed
Python signatures and the ``schemas.py`` Pydantic models, rather than the
low-level ``mcp.server.lowlevel.Server`` API's hand-written JSON-RPC
handlers -- simpler and directly testable (a FastMCP tool is just the
underlying Python coroutine, see ``mcp/tools.py``'s docstring).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ragmonk import __version__

from . import tools

INSTRUCTIONS = (
    "RagMonk exposes code and document knowledge already indexed locally "
    "on this machine (see `ragmonk index`). Start with ragmonk_explore "
    "for a natural-language or identifier query -- it is the primary tool "
    "and runs RagMonk's deterministic query planner. The other read-only "
    "tools (ragmonk_search/symbol/callers/callees/impact/documents/status) "
    "are narrower, single-purpose lookups; all 8 of them return a bounded, "
    "versioned JSON result (schema_version/ok/error) and never call an LLM "
    "or the network -- results are retrieved/structured data for the "
    "calling agent to reason over. ragmonk_ask is the one exception: it "
    "calls the locally configured `ai:` provider (which may be a real "
    "cloud endpoint, gated by privacy.external_ai_allowed) to synthesize "
    "an answer over the same evidence ragmonk_explore would return -- "
    "prefer ragmonk_explore when the calling agent can reason over "
    "evidence itself; use ragmonk_ask only when a synthesized natural-"
    "language answer is specifically wanted."
)

# The 8 retrieval-only tools only read the local index -- never write,
# never touch the network -- so one shared annotation set covers every
# registration below except ragmonk_ask.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# ragmonk_ask calls out to whatever ``ai:`` provider is configured,
# which may be a real network endpoint -- the opposite of every other
# tool's openWorldHint=False, and not idempotent in the sense an LLM
# response is not guaranteed identical across calls.
_ASK_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

_TOOLS: tuple[tuple[str, str], ...] = (
    (
        "ragmonk_explore",
        "Primary retrieval tool: plan + run the best deterministic strategies for a "
        "query and assemble a budgeted evidence package.",
    ),
    ("ragmonk_search", "Lexical search across indexed code and documents."),
    ("ragmonk_symbol", "Look up a code symbol by name or fully qualified name."),
    ("ragmonk_callers", "List entities that call the given symbol (incoming CALLS edges)."),
    ("ragmonk_callees", "List entities the given symbol calls (outgoing CALLS edges)."),
    (
        "ragmonk_impact",
        "Blast-radius impact analysis for a symbol: callers, callees, tests, documentation.",
    ),
    ("ragmonk_documents", "List indexed documents, optionally filtered to one source."),
    ("ragmonk_status", "Indexing status: sources, per-status file counts, queue depth."),
)

# Registered separately from _TOOLS above since it takes _ASK_ANNOTATIONS,
# not _READ_ONLY -- see that annotation's own comment.
_ASK_TOOL = (
    "ragmonk_ask",
    "Ask a natural-language question, answered by the configured AI provider "
    "grounded in the same evidence ragmonk_explore would return. Calls out to "
    "a real (possibly cloud) AI provider -- unlike every other tool here.",
)


def build_server() -> FastMCP:
    """Constructs the FastMCP server and registers all 9 tools. Pure and
    side-effect free (no stdio, no sockets) so tests can call it directly.
    """
    server = FastMCP(name=f"ragmonk v{__version__}", instructions=INSTRUCTIONS)
    for tool_name, description in _TOOLS:
        server.add_tool(
            getattr(tools, tool_name),
            name=tool_name,
            description=description,
            annotations=_READ_ONLY,
        )
    ask_name, ask_description = _ASK_TOOL
    server.add_tool(
        getattr(tools, ask_name),
        name=ask_name,
        description=ask_description,
        annotations=_ASK_ANNOTATIONS,
    )
    return server


def run_stdio() -> None:
    """Blocks running the stdio MCP server loop. Not covered by tests
    (would require a real client speaking JSON-RPC over stdio); ``cli/
    serve.py`` is the only caller, kept as this one thin, deliberately
    untested line so ``build_server``'s construction stays testable
    in isolation.
    """
    build_server().run(transport="stdio")
