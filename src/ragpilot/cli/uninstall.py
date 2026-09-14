"""``ragpilot uninstall [--keep-data] [--yes] [--json]``.

Removes the installed RAGpilot application and, by default, all of its
data (``RAGPILOT_HOME``) too -- pass ``--keep-data`` to remove only the
application. Prompts for confirmation before deleting anything unless
``--yes`` is given. See ``ops/uninstall.py`` for the full sequence.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ragpilot.core import paths
from ragpilot.ops import uninstall as uninstall_ops
from ragpilot.update.installer import InstallMethod

from ._common import cli_command, console, print_json


def _app_description(plan: uninstall_ops.UninstallPlan) -> str:
    if not plan.can_auto_remove_app:
        return "the application (see manual instructions below)"
    if plan.method is InstallMethod.INSTALL_SCRIPT:
        paths_str = ", ".join(str(p) for p in plan.app_paths)
        return f"the installed application: {paths_str}"
    return f"the ragpilot package (`{plan.method.value} uninstall`)"


@cli_command
def uninstall(
    keep_data: Annotated[
        bool, typer.Option("--keep-data", help="Remove only the application; keep RAGPILOT_HOME.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    home = paths.runtime_dir()
    plan = uninstall_ops.plan_uninstall(home)

    lines = [_app_description(plan)]
    if not keep_data:
        lines.append(f"all data under {home} (databases, config, backups, logs)")

    if not yes:
        console.print("[bold]This will permanently remove:[/bold]")
        for line in lines:
            console.print(f"  - {line}")
        if not typer.confirm("Proceed?"):
            console.print("Aborted; nothing was removed.")
            raise typer.Exit(code=0)

    # Application removal must run before the data purge, not after: for
    # both install.sh and install.ps1's default layout, the venv lives
    # *inside* RAGPILOT_HOME (they share the same default root), so
    # purging data first can delete the very interpreter `pip uninstall`
    # (run via sys.executable) is about to need, breaking it outright.
    app_removed = uninstall_ops.remove_application(plan)
    data_purged = False if keep_data else uninstall_ops.purge_home(home)

    result: dict[str, Any] = {
        "install_method": plan.method.value,
        "data_purged": data_purged,
        "app_removed": app_removed,
        "manual_instructions": plan.manual_instructions,
    }

    if json_output:
        print_json(result)
        return

    if data_purged:
        console.print(f"[green]✓[/green] Removed data under {home}")
    if app_removed:
        console.print("[green]✓[/green] Removed the application")
    if plan.manual_instructions:
        console.print(f"\n[yellow]![/yellow] {plan.manual_instructions}")
    if not data_purged and not app_removed and not plan.manual_instructions:
        console.print("Nothing to remove.")
