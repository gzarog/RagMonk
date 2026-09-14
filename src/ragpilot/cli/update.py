"""``ragpilot update [check|status|install]``.

``ragpilot update`` alone behaves like ``ragpilot update check`` -- the
single most useful thing to do with no arguments, mirroring tools like
``brew outdated``/``npm outdated``.

Distinct from ``ragpilot upgrade`` (``cli/upgrade.py``), which applies
database *schema* migrations to the already-installed RAGpilot: these
commands discover and install a newer RAGpilot *release* itself. See
``update/__init__.py``'s module docstring.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ragpilot.core import paths
from ragpilot.core.errors import EXIT_HEALTH_CHECK_FAILURE, RagpilotError
from ragpilot.update import cache, checker, installer, versioning
from ragpilot.update.models import UpdateCache

from ._common import cli_command, console, print_json

app = typer.Typer(help="Check for and install RAGpilot updates.")


@app.callback(invoke_without_command=True)
def update_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        check()


@app.command("check", help="Query GitHub for the latest release and refresh the local cache.")
@cli_command
def check(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    # Always queries GitHub -- never rate-limited by the local cache; that
    # gate belongs to a later phase's automatic background check, not this
    # explicit command.
    home = paths.ensure_runtime_layout()
    installed = versioning.installed_version()

    try:
        latest = checker.fetch_latest_release()
    except checker.UpdateCheckError as exc:
        raise RagpilotError(f"could not check for updates: {exc}") from exc

    update_available = versioning.is_newer(latest.version, installed)
    cache.write_cache(
        home,
        UpdateCache(
            last_checked=cache.now_iso(),
            installed_version=installed,
            latest_version=latest.version,
            release_url=latest.html_url,
        ),
    )

    if json_output:
        print_json(
            {
                "installed_version": installed,
                "latest_version": latest.version,
                "update_available": update_available,
                "release_url": latest.html_url,
            }
        )
        return

    console.print(f"Installed: {installed}")
    console.print(f"Latest:    {latest.version}")
    console.print()
    if update_available:
        console.print("[bold]Update available.[/bold] Run:")
        console.print()
        console.print("    ragpilot update install")
    else:
        console.print(f"RAGpilot {installed} is up to date.")


@app.command("status", help="Show the cached update status (run `update check` to refresh it).")
@cli_command
def status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    # Reads the local cache only -- never talks to GitHub.
    home = paths.ensure_runtime_layout()
    installed = versioning.installed_version()
    cached = cache.read_cache(home)

    if cached is None:
        if json_output:
            print_json(
                {
                    "installed_version": installed,
                    "latest_version": None,
                    "status": "unknown",
                    "last_checked": None,
                }
            )
            return
        console.print(f"Installed: {installed}")
        console.print("Latest:    unknown -- run `ragpilot update check`")
        return

    up_to_date = not versioning.is_newer(cached.latest_version, installed)
    status_label = "up to date" if up_to_date else "update available"

    if json_output:
        print_json(
            {
                "installed_version": installed,
                "latest_version": cached.latest_version,
                "status": status_label,
                "last_checked": cached.last_checked,
                "release_url": cached.release_url,
            }
        )
        return

    console.print(f"Installed: {installed}")
    console.print(f"Latest:    {cached.latest_version}")
    console.print(f"Status:    {status_label}")


@app.command("install", help="Check for, and install, the latest release.")
@cli_command
def install(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    home = paths.ensure_runtime_layout()
    try:
        outcome = installer.install_latest(home)
    except installer.UpdateInstallError as exc:
        raise RagpilotError(str(exc)) from exc

    if json_output:
        print_json(
            {
                "installed_version": outcome.installed_version,
                "upgraded": outcome.upgraded,
                "migrations_applied": outcome.migrations_applied,
                "healthy": outcome.healthy,
            }
        )
    elif not outcome.upgraded:
        console.print(f"RAGpilot {outcome.installed_version} is already up to date.")
    else:
        console.print(f"[green]✓[/green] Installed RAGpilot {outcome.installed_version}")
        if outcome.migrations_applied:
            console.print("[green]✓[/green] Database migrations complete")
        else:
            console.print(
                "[yellow]![/yellow] Database migrations did not complete cleanly -- "
                "run `ragpilot upgrade` to retry"
            )
        if outcome.healthy:
            console.print("[green]✓[/green] Health check passed")
        else:
            console.print(
                "[yellow]![/yellow] Health check reported issues -- see `ragpilot doctor`"
            )
        console.print(f"\nRAGpilot {outcome.installed_version} is ready.")

    if outcome.upgraded and not (outcome.migrations_applied and outcome.healthy):
        raise typer.Exit(code=EXIT_HEALTH_CHECK_FAILURE)
