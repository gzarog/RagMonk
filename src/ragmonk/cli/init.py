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

Before writing a server-mode config, a real (not mocked) HTTP GET
preflight against ``--storage-url`` is performed with a short timeout: no
file is written if it fails, and no partially-written config is ever left
behind (the write only happens after a successful preflight).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Annotated

import typer

from ragmonk.backends.factory import credential_env_vars
from ragmonk.core import paths
from ragmonk.core.config import (
    RagMonkConfig,
    ServerStorageConfig,
    StorageConfig,
    load_config,
    write_user_config,
)
from ragmonk.core.errors import UsageError
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect

from ._common import cli_command, console

_PREFLIGHT_TIMEOUT_SECONDS = 5.0


def _preflight_server_url(url: str, *, engine: str, verify_tls: bool) -> None:
    """A real, minimal HTTP GET against ``url``, raising ``UsageError`` on
    any failure (unreachable host, timeout, non-2xx/3xx response). This is
    a placeholder for a future engine-specific cluster health/version
    check -- it proves the URL is reachable, nothing more.
    """
    if not url:
        raise UsageError("--storage-url is required when --storage-mode=server")

    username_var, password_var, api_key_var = credential_env_vars(engine)
    import os

    headers: dict[str, str] = {}
    api_key = os.environ.get(api_key_var)
    username = os.environ.get(username_var)
    password = os.environ.get(password_var)
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    elif username and password:
        import base64

        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    ctx = None
    if not verify_tls:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(
            request, timeout=_PREFLIGHT_TIMEOUT_SECONDS, context=ctx
        ) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UsageError(
            f"storage preflight check failed: could not reach {url!r} ({exc}); "
            "no config file was written"
        ) from exc

    if not (200 <= status < 400):
        raise UsageError(
            f"storage preflight check failed: {url!r} responded with HTTP {status}; "
            "no config file was written"
        )


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
            _preflight_server_url(
                storage_url, engine=storage_engine, verify_tls=storage_verify_tls
            )
            storage = StorageConfig(
                mode="server",
                server=ServerStorageConfig(
                    engine=storage_engine,  # type: ignore[arg-type]
                    url=storage_url,
                    index_prefix=storage_index_prefix,
                    verify_tls=storage_verify_tls,
                ),
            )
            console.print(
                f"[green]Storage preflight OK[/green] -- {storage_engine} at {storage_url}"
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
