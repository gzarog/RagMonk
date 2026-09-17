"""``ragmonk restore ARCHIVE [--json]``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from ragmonk.core import paths
from ragmonk.ops.restore import restore_backup

from ._common import cli_command, console, print_json


@cli_command
def restore(
    archive: Annotated[
        str, typer.Argument(help="Path to a backup archive created by 'ragmonk backup'.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    # Deliberately no `AppContext.bootstrap()` here: that would open (and
    # auto-migrate) connections against the *pre-restore* live state right
    # before that state is about to be swapped out from under them --
    # `restore_backup` works from plain paths instead, so nothing holds a
    # handle on `sources.db`/`projects/` while the atomic swap happens.
    home = paths.ensure_runtime_layout()
    report = restore_backup(Path(archive), home=home)

    data: dict[str, Any] = {
        "archive": report.archive,
        "projects_restored": report.projects_restored,
        "daemon_was_running": report.daemon_was_running,
        "daemon_restarted": report.daemon_restarted,
    }
    if json_output:
        print_json(data)
        return

    console.print(f"[bold green]Restored[/bold green] from {report.archive}")
    console.print(f"Projects restored: {len(report.projects_restored)}")
    if report.daemon_was_running:
        state = "restarted" if report.daemon_restarted else "left stopped"
        console.print(f"Daemon was running before restore; {state}")
