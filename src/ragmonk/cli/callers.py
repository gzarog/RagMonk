"""``ragmonk callers NAME [--max-depth N] [--limit N] [--json]``."""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.code.graph import DEFAULT_LIMIT, DEFAULT_MAX_DEPTH, traverse_symbol
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import RelationshipType

from ._code_common import edge_to_dict, match_to_dict, no_matches_message, render_edges_table
from ._common import cli_command, console, print_json


@cli_command
def callers(
    name: Annotated[str, typer.Argument(help="Symbol name or fully qualified name.")],
    max_depth: Annotated[int, typer.Option("--max-depth", min=1, max=10)] = DEFAULT_MAX_DEPTH,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = DEFAULT_LIMIT,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Entities that CALL ``name`` (incoming CALLS edges)."""
    with AppContext.bootstrap() as ctx:
        matches, edges = traverse_symbol(
            ctx,
            name,
            direction="incoming",
            relationship_types=(RelationshipType.CALLS,),
            max_depth=max_depth,
            limit=limit,
        )
        if json_output:
            print_json(
                {
                    "query": name,
                    "matches": [match_to_dict(m) for m in matches],
                    "edges": [edge_to_dict(e) for e in edges],
                }
            )
            return
        if not matches and not edges:
            console.print(f"[yellow]{no_matches_message(name)}[/yellow]")
            return
        render_edges_table(edges)
