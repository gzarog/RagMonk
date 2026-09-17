"""``ragmonk symbol NAME [--json]``."""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.code.graph import find_symbol_matches
from ragmonk.core.lifecycle import AppContext

from ._code_common import match_to_dict, no_matches_message, render_matches_table
from ._common import cli_command, console, print_json


@cli_command
def symbol(
    name: Annotated[str, typer.Argument(help="Symbol name or fully qualified name.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        matches = find_symbol_matches(ctx, name)
        if json_output:
            print_json({"query": name, "matches": [match_to_dict(m) for m in matches]})
            return
        if not matches:
            console.print(f"[yellow]{no_matches_message(name)}[/yellow]")
            return
        render_matches_table(matches)
