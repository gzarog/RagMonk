"""``ragmonk source add|list|info|enable|disable|remove``."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from ragmonk.core.lifecycle import AppContext
from ragmonk.sources.registry import SourceRegistry

from ._common import cli_command, console

app = typer.Typer(no_args_is_help=True, help="Manage registered sources.")


def _confirm_removal(source_id: str, source_path: str, project_dir: str) -> bool:
    console.print("[bold]This will permanently remove:[/bold]")
    console.print(f"  - source [bold]{source_id}[/bold] ({source_path}) from the registry")
    console.print(f"  - its indexed data at {project_dir}")
    console.print("[dim]The original files at the source path are never touched.[/dim]")
    return typer.confirm("Proceed?")


@app.command("add")
@cli_command
def add(
    path: Annotated[str, typer.Argument(help="Directory to register as a source.")],
    include: Annotated[list[str], typer.Option("--include", help="Include glob pattern.")] = [],  # noqa: B006
    exclude: Annotated[list[str], typer.Option("--exclude", help="Exclude glob pattern.")] = [],  # noqa: B006
) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(path, include_patterns=list(include), exclude_patterns=list(exclude))
        console.print(f"[bold green]Added source[/bold green] {source.id} -> {source.path}")


@app.command("list")
@cli_command
def list_sources() -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        sources = registry.list()
        table = Table("ID", "Path", "Type", "Enabled", "Status", "Last Scan")
        for source in sources:
            table.add_row(
                source.id,
                source.path,
                source.source_type.value,
                "yes" if source.enabled else "no",
                source.status.value,
                source.last_scan_at or "-",
            )
        console.print(table)


@app.command("info")
@cli_command
def info(source_id: Annotated[str, typer.Argument()]) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.get(source_id)
        console.print_json(data=source.model_dump(mode="json"))


@app.command("enable")
@cli_command
def enable(source_id: Annotated[str, typer.Argument()]) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.set_enabled(source_id, True)
        console.print(f"[green]Enabled[/green] {source_id}")


@app.command("disable")
@cli_command
def disable(source_id: Annotated[str, typer.Argument()]) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.set_enabled(source_id, False)
        console.print(f"[yellow]Disabled[/yellow] {source_id}")


@app.command("remove")
@cli_command
def remove(
    source_id: Annotated[str, typer.Argument()],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.get(source_id)
        # Checked before the confirmation prompt (and, redundantly, again
        # inside `registry.remove`) so a running daemon fails fast here
        # rather than the CLI hanging on `acquire_lock` below behind a
        # daemon pass that keeps re-triggering itself.
        registry.assert_no_active_daemon()
        project_dir = registry.project_dir_for(source)

        if not yes and not _confirm_removal(source.id, source.path, str(project_dir)):
            console.print("Aborted; nothing was removed.")
            raise typer.Exit(code=0)

        lock = ctx.acquire_lock("index")
        try:
            outcome = registry.remove(source_id)
        finally:
            lock.release()

        console.print(f"[bold red]Removed[/bold red] {outcome.source.id} ({outcome.source.path})")
        if outcome.project_dir_deleted:
            console.print(f"  deleted indexed data at {outcome.project_dir}")
