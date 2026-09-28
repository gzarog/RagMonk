"""``ragmonk index`` -- scan registered sources and process pending files."""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.backends.factory import redact_urls_in_text
from ragmonk.core.errors import IndexingPartialFailureError, RunLockTimeoutError
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry

from ._common import cli_command, console


@cli_command
def index(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only index this source id.")
    ] = None,
) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        if source_id is not None:
            sources = [registry.get(source_id)]
        else:
            sources = registry.list(enabled_only=True)

        if not sources:
            console.print("[yellow]No enabled sources to index.[/yellow]")
            return

        total_failed = 0
        failed_sources = 0
        processors = build_processor_registry(ctx.config)
        for source in sources:
            # Per-source isolation: one source's failure must not stop
            # independent sources from being indexed. The runner has
            # already recorded last_error (and logged the failed
            # stage); the command still fails once all are attempted.
            try:
                # Lock scope is one source pass: released between sources so
                # another indexing actor can interleave, never overlapped.
                with ctx.index_lock(operation="index", source_id=source.id):
                    pass_result = run_source_pass(ctx, source, processors)
            except RunLockTimeoutError as exc:
                failed_sources += 1
                console.print(f"[red]{source.id}[/red] {source.path}: blocked: {exc}")
                continue
            except Exception as exc:
                failed_sources += 1
                console.print(
                    f"[red]{source.id}[/red] {source.path}: source pass failed: "
                    f"{type(exc).__name__}: {redact_urls_in_text(str(exc))}"
                )
                continue
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
            if result.scan_incomplete:
                console.print(
                    f"[yellow]{source.id}[/yellow]: scan was incomplete "
                    f"({len(result.scan_errors)} unreadable path(s)); "
                    "deletion reconciliation skipped this pass, will retry"
                )

        attempted = len(sources)
        if failed_sources:
            console.print(
                f"Index complete with failures: {attempted} source(s) attempted, "
                f"{attempted - failed_sources} completed, {failed_sources} source(s) failed."
            )
        else:
            console.print(
                f"Index complete: {attempted} source(s) processed, {attempted} succeeded, 0 failed."
            )
        if failed_sources or total_failed:
            parts = []
            if failed_sources:
                parts.append(f"{failed_sources} source(s) failed")
            if total_failed:
                parts.append(f"{total_failed} file(s) failed to index")
            raise IndexingPartialFailureError(
                "; ".join(parts) + "; see 'ragmonk doctor' for details"
            )
