"""``ragpilot index`` -- scan registered sources and process pending files."""

from __future__ import annotations

from typing import Annotated

import typer

from ragpilot.core.errors import IndexingPartialFailureError
from ragpilot.core.lifecycle import AppContext
from ragpilot.indexing.runner import build_processor_registry, run_source_pass
from ragpilot.sources.registry import SourceRegistry

from ._common import cli_command, console


@cli_command
def index(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only index this source id.")
    ] = None,
) -> None:
    with AppContext.bootstrap() as ctx:
        lock = ctx.acquire_lock("index")
        try:
            registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
            if source_id is not None:
                sources = [registry.get(source_id)]
            else:
                sources = registry.list(enabled_only=True)

            if not sources:
                console.print("[yellow]No enabled sources to index.[/yellow]")
                return

            total_failed = 0
            processors = build_processor_registry(ctx.config)
            for source in sources:
                pass_result = run_source_pass(ctx, source, processors)
                result = pass_result.result

                if result.source_offline:
                    console.print(
                        f"[yellow]{source.id}[/yellow] {source.path}: "
                        f"source unreachable ({result.offline_reason}); "
                        "marked OFFLINE, skipped deletion reconciliation"
                    )
                    continue

                total_failed += result.failed
                if pass_result.became_online:
                    console.print(
                        f"[green]{source.id}[/green] {source.path}: "
                        "source reachable again; back to ACTIVE"
                    )
                console.print(
                    f"[bold]{source.id}[/bold] {source.path}: "
                    f"scanned={result.scanned} new={result.new} changed={result.changed} "
                    f"unchanged={result.unchanged} moved={result.moved} "
                    f"deleted={result.deleted} "
                    f"indexed={result.indexed} skipped={result.skipped_limit} "
                    f"failed={result.failed} linked={pass_result.linked} "
                    f"embedded={pass_result.embedded}"
                )

            if total_failed:
                raise IndexingPartialFailureError(
                    f"{total_failed} file(s) failed to index; see 'ragpilot doctor' for details"
                )
        finally:
            lock.release()
