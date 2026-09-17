"""``ragmonk references NAME [--max-depth N] [--limit N] [--json]``."""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval.graph import DEFAULT_LIMIT, DEFAULT_MAX_DEPTH
from ragmonk.retrieval.graph import references as graph_references

from ._code_common import edge_to_dict, match_to_dict, no_matches_message, render_edges_table
from ._common import cli_command, console, print_json


@cli_command
def references(
    name: Annotated[str, typer.Argument(help="Symbol name or fully qualified name.")],
    max_depth: Annotated[int, typer.Option("--max-depth", min=1, max=10)] = DEFAULT_MAX_DEPTH,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = DEFAULT_LIMIT,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """All CALLS/IMPORTS/REFERENCES edges touching ``name``, either way."""
    with AppContext.bootstrap() as ctx:
        matches, edges = graph_references(ctx, name, max_depth=max_depth, limit=limit)

        if json_output:
            print_json(
                {
                    "query": name,
                    "matches": [match_to_dict(m) for m in matches],
                    "edges": [edge_to_dict(e) for e in edges],
                }
            )
            return
        if not matches:
            console.print(f"[yellow]{no_matches_message(name)}[/yellow]")
            return
        render_edges_table(edges)
