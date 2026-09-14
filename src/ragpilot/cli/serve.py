"""``ragpilot serve --mcp`` -- start the stdio MCP server.

``--mcp`` is required and is (deliberately) the only transport this phase
implements; the optional local REST API (``api:`` config section) is
Phase 8's job. This command only checks ``mcp.enabled`` and hands off to
the blocking stdio loop -- server construction itself lives in
``mcp/server.py`` and is unit-tested there, independent of this command.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ragpilot.core.errors import ConfigError, UsageError
from ragpilot.core.lifecycle import AppContext

from ._common import cli_command, error_console


@cli_command
def serve(
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Start the stdio MCP server (the only transport this phase implements).",
        ),
    ] = False,
) -> None:
    if not mcp:
        raise UsageError(
            "ragpilot serve currently requires --mcp "
            "(the REST API lands in a later phase)."
        )

    with AppContext.bootstrap() as ctx:
        if not ctx.config.mcp.enabled:
            raise ConfigError(
                "the MCP server is disabled (mcp.enabled=false in config); "
                "run `ragpilot config set mcp.enabled true` to enable it."
            )

    # Never print to stdout here: stdout is the MCP stdio transport's
    # JSON-RPC channel once run_stdio() starts, so even a startup banner
    # has to go to stderr.
    error_console.print("[bold]RAGpilot MCP server starting on stdio...[/bold]")
    # Deferred import (CLI performance improvement plan, Phase 2): the mcp
    # SDK must only load for `ragpilot serve --mcp`, never for any other
    # command that happens to import this module.
    from ragpilot.mcp.server import run_stdio

    run_stdio()
