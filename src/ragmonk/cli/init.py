"""``ragmonk init`` -- bootstrap the RagMonk runtime directory."""

from __future__ import annotations

from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig, load_config, write_user_config
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect

from ._common import cli_command, console


@cli_command
def init() -> None:
    home = paths.ensure_runtime_layout()
    config_path = paths.user_config_path(home)
    if not config_path.is_file():
        write_user_config(RagMonkConfig(), home=home)
        console.print(f"Wrote default configuration to [cyan]{config_path}[/cyan]")
    else:
        load_config(home=home)  # validate the existing config, surfaces ConfigError early
        console.print(f"Using existing configuration at [cyan]{config_path}[/cyan]")

    conn = connect(paths.sources_db_path(home))
    try:
        apply_migrations(conn, "sources")
    finally:
        conn.close()

    console.print(f"[bold green]RagMonk initialized[/bold green] at {home}")
