"""``ragmonk backup [PATH] [--json]``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from ragmonk.core.lifecycle import AppContext
from ragmonk.ops.backup import create_backup
from ragmonk.sources.registry import SourceRegistry

from ._common import cli_command, console, print_json


@cli_command
def backup(
    dest: Annotated[
        str | None,
        typer.Argument(
            help="Output archive path (default: <home>/backups/ragmonk-backup-<timestamp>.tar.gz)."
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        # The "index" lock -- the same one `ragmonk index` and each of
        # the daemon's per-pass runs hold (core/lifecycle.py, service/
        # daemon.py) -- serializes this backup against any concurrent
        # writer rather than racing it. SQLite's online backup API
        # (ops/backup.py) is itself safe against a concurrent writer, so
        # this is a belt-and-suspenders guarantee of a fully quiescent
        # snapshot, not a strict requirement.
        lock = ctx.acquire_lock("index")
        try:
            sources = SourceRegistry(ctx.sources_conn, home=ctx.home).list()
            archive_path, manifest = create_backup(
                home=ctx.home, sources=sources, dest=Path(dest) if dest else None
            )
        finally:
            lock.release()

    data: dict[str, Any] = {
        "archive": str(archive_path),
        "ragmonk_version": manifest.ragmonk_version,
        "created_at": manifest.created_at,
        "sources_schema_version": manifest.sources_schema_version,
        "projects": manifest.projects,
    }
    if json_output:
        print_json(data)
        return

    console.print(f"[bold green]Backup created[/bold green] {archive_path}")
    console.print(f"Projects included: {len(manifest.projects)}")
