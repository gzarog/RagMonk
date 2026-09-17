"""``ragmonk rebuild [--source ID] [--json]``."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.core.errors import IndexingPartialFailureError
from ragmonk.core.lifecycle import AppContext
from ragmonk.ops.rebuild import rebuild as run_rebuild

from ._common import cli_command, console, print_json


@cli_command
def rebuild(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only rebuild this source id.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        lock = ctx.acquire_lock("index")
        try:
            outcomes = run_rebuild(ctx, source_id=source_id)
        finally:
            lock.release()

    data: dict[str, Any] = {
        "sources": [
            {
                "id": o.source.id,
                "path": o.source.path,
                "scanned": o.result.scanned,
                "indexed": o.result.indexed,
                "failed": o.result.failed,
                "linked": o.linked,
            }
            for o in outcomes
        ]
    }
    total_failed = sum(o.result.failed for o in outcomes)

    if json_output:
        print_json(data)
    else:
        for o in outcomes:
            console.print(
                f"[bold]{o.source.id}[/bold] {o.source.path}: rebuilt "
                f"scanned={o.result.scanned} indexed={o.result.indexed} "
                f"failed={o.result.failed} linked={o.linked}"
            )

    if total_failed:
        raise IndexingPartialFailureError(
            f"{total_failed} file(s) failed to (re)index during rebuild; "
            "see 'ragmonk doctor' for details"
        )
