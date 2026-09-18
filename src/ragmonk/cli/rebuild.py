"""``ragmonk rebuild [--source ID] [--json]``."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.core.errors import IndexingPartialFailureError, UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.ops.rebuild import rebuild as run_rebuild

from ._common import cli_command, console, print_json


@cli_command
def rebuild(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only rebuild this source id.")
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help=(
                "Recoverable rebuild from source files: the existing index is kept "
                "as a backup until the fresh rebuild succeeds, and restored if it fails. "
                "Use this to rebuild after the exact tokenizer changed the index identity."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the confirmation prompt for --fresh.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if fresh and not yes and not json_output:
        confirmed = typer.confirm(
            "rebuild --fresh will re-index every selected source from its source files "
            "(the old index is kept as a backup until the rebuild succeeds). Continue?"
        )
        if not confirmed:
            raise UsageError("aborted: rebuild --fresh not confirmed (pass --yes to skip)")

    with AppContext.bootstrap() as ctx:
        lock = ctx.acquire_lock("index")
        try:
            outcomes = run_rebuild(ctx, source_id=source_id, fresh=fresh)
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
