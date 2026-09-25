"""``ragmonk init`` -- bootstrap the RagMonk runtime directory.

Storage backend abstraction plan, Phase 1 (P2): adds optional storage-mode
selection for a *new* config -- local (default, unchanged from before this
phase) or server (OpenSearch/Elasticsearch connection scaffolding; no
adapter exists yet, see ``ragmonk.backends.factory``). With no flags, and
for any *existing* config, behavior is byte-for-byte what it was before
this phase: a local config is written (or the existing config is merely
validated), same as always.

Server-mode init never accepts or persists a credential -- only
``--storage-url``/``--storage-engine``/``--storage-index-prefix``/
``--storage-verify-tls`` (non-secret connection shape). Credentials are
read only from ``RAGMONK_<ENGINE>_USERNAME``/``_PASSWORD``/``_API_KEY`` at
call time, purely to include as a preflight auth header if a caller sets
them -- never written to the config file.

Before writing a server-mode config, the selected engine is validated
with its *real* client (completion plan F5 -- see
``ragmonk.backends.validation``): reachability, authentication, engine
identity (OpenSearch must not pass as Elasticsearch or vice versa, and a
generic HTTP 200 server passes neither), supported version, and
non-destructive permission checks. No file is written unless every check
passes, and no error message ever contains a credential.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.core import paths
from ragmonk.core.config import (
    RagMonkConfig,
    ServerStorageConfig,
    StorageConfig,
    load_config,
    url_has_userinfo,
    write_user_config,
)
from ragmonk.core.errors import UsageError
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect

from ._common import cli_command, console


def _validated_server_storage(
    *, engine: str, url: str, index_prefix: str, verify_tls: bool
) -> StorageConfig:
    """Builds and *really* validates the server storage config (see
    module docstring). Raises ``UsageError`` -- never a raw pydantic
    error, whose text would echo the URL -- on any failure.
    """
    from ragmonk.backends.factory import redact_url
    from ragmonk.backends.validation import validate_server_config

    if not url:
        raise UsageError("--storage-url is required when --storage-mode=server")
    if url_has_userinfo(url):
        raise UsageError(
            "--storage-url must not contain credentials (user-info such as "
            "'user:password@'); set RAGMONK_OPENSEARCH_USERNAME/_PASSWORD/_API_KEY or "
            "RAGMONK_ELASTICSEARCH_USERNAME/_PASSWORD/_API_KEY instead; "
            "no config file was written"
        )
    if engine not in ("opensearch", "elasticsearch"):
        raise UsageError(
            f"unknown --storage-engine {engine!r}; expected 'opensearch' or 'elasticsearch'"
        )
    try:
        server = ServerStorageConfig(
            engine=engine,  # type: ignore[arg-type]
            url=url,
            index_prefix=index_prefix,
            verify_tls=verify_tls,
        )
    except Exception as exc:
        raise UsageError(f"invalid server storage options ({type(exc).__name__})") from None
    try:
        result = validate_server_config(server)
    except UsageError as exc:
        raise UsageError(
            f"storage validation failed: {exc}; no config file was written"
        ) from None
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")
    console.print(
        f"[green]Storage validation OK[/green] -- {result.engine} {result.version} "
        f"at {redact_url(url)}"
    )
    return StorageConfig(mode="server", server=server)


@cli_command
def init(
    storage_mode: Annotated[
        str,
        typer.Option(
            "--storage-mode",
            help="Storage backend mode for a *new* config: 'local' (default) or 'server'.",
        ),
    ] = "local",
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Shorthand for --storage-mode=local (the default; explicit opt-in).",
        ),
    ] = False,
    storage_engine: Annotated[
        str,
        typer.Option(
            "--storage-engine",
            help="Server engine when --storage-mode=server: 'opensearch' or 'elasticsearch'.",
        ),
    ] = "opensearch",
    storage_url: Annotated[
        str,
        typer.Option("--storage-url", help="Server URL when --storage-mode=server."),
    ] = "",
    storage_index_prefix: Annotated[
        str,
        typer.Option(
            "--storage-index-prefix", help="Index name prefix when --storage-mode=server."
        ),
    ] = "ragmonk",
    storage_verify_tls: Annotated[
        bool,
        typer.Option(
            "--storage-verify-tls/--storage-no-verify-tls",
            help="Verify TLS certificates when --storage-mode=server.",
        ),
    ] = True,
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive/--no-interactive",
            help="Prompt for storage mode/options for a new config instead of using flags.",
        ),
    ] = False,
) -> None:
    home = paths.ensure_runtime_layout()
    config_path = paths.user_config_path(home)

    if not config_path.is_file():
        if local and storage_mode != "local":
            raise UsageError("--local conflicts with --storage-mode=server")

        if interactive:
            storage_mode = typer.prompt(
                "Storage mode", default="local", type=str
            ).strip().lower()
            if storage_mode == "server":
                storage_engine = typer.prompt(
                    "Server engine", default=storage_engine, type=str
                ).strip().lower()
                storage_url = typer.prompt("Server URL", default=storage_url or "").strip()
                storage_index_prefix = typer.prompt(
                    "Index prefix", default=storage_index_prefix
                ).strip()
                storage_verify_tls = typer.confirm(
                    "Verify TLS certificates?", default=storage_verify_tls
                )

        if storage_mode not in ("local", "server"):
            raise UsageError(
                f"unknown --storage-mode {storage_mode!r}; expected 'local' or 'server'"
            )

        if storage_mode == "server":
            storage = _validated_server_storage(
                engine=storage_engine,
                url=storage_url,
                index_prefix=storage_index_prefix,
                verify_tls=storage_verify_tls,
            )
        else:
            # Default/explicit local: identical resulting config shape to
            # before this phase (storage.mode defaults to "local" either way).
            storage = StorageConfig()

        write_user_config(RagMonkConfig(storage=storage), home=home)
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
