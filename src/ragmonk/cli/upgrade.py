"""``ragmonk upgrade [--json]``."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragmonk.core import paths
from ragmonk.core.errors import EXIT_HEALTH_CHECK_FAILURE
from ragmonk.ops.upgrade import run_upgrade

from ._common import cli_command, console, print_json


@cli_command
def upgrade(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    home = paths.ensure_runtime_layout()
    report = run_upgrade(home=home)

    data: dict[str, Any] = {
        "pending": report.pending,
        "backup_archive": report.backup_archive,
        "databases": [
            {"name": d.name, "before": d.before, "after": d.after} for d in report.databases
        ],
        "healthy_after": report.healthy_after,
    }

    if json_output:
        print_json(data)
    else:
        if not report.pending:
            console.print("[green]Already up to date[/green]; no pending migrations.")
        else:
            console.print(f"[bold]Backup taken[/bold] before upgrading: {report.backup_archive}")
        for d in report.databases:
            marker = "->" if d.before != d.after else "=="
            console.print(f"  {d.name}: v{d.before} {marker} v{d.after}")
        if report.healthy_after:
            console.print("[green]Post-upgrade health check: OK[/green]")
        else:
            restore_hint = (
                f"'ragmonk restore {report.backup_archive}'"
                if report.backup_archive
                else "the most recent 'ragmonk backup' archive"
            )
            console.print(
                "[bold red]Post-upgrade health check: UNHEALTHY[/bold red] -- "
                f"see 'ragmonk doctor' for detail. No automatic rollback was attempted; "
                f"if needed, restore the pre-upgrade backup with {restore_hint}."
            )

    if not report.healthy_after:
        raise typer.Exit(code=EXIT_HEALTH_CHECK_FAILURE)
