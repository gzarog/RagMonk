"""``ragmonk ui`` -- start the local administration web interface.

Admin UI plan, Phase 1 (§4.2/§4.3): serve the FastAPI admin app on
``127.0.0.1`` by default and open the browser. The command blocks until
``Ctrl+C``, which shuts Uvicorn (and, via the app's lifespan, the
``AppContext``) down cleanly.
"""

from __future__ import annotations

import contextlib
import threading
import time
import webbrowser
from typing import Annotated

import typer

from ._common import cli_command, console


@cli_command
def ui(
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind. Defaults to localhost.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser automatically.")
    ] = False,
) -> None:
    # Imported here, not at module top, so ``ragmonk --help`` and every
    # non-UI command stay free of the FastAPI/Uvicorn import cost.
    import uvicorn

    from ragmonk.ui.app import create_app

    url = f"http://{host}:{port}"
    app = create_app(host=host, port=port)

    console.print("[bold]RagMonk Admin UI[/bold]")
    console.print(f"URL: {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # §14.1/§14.5: remote binding is not the local-first default and
        # has no auth model yet -- warn loudly rather than silently expose.
        console.print(
            "[bold yellow]Warning:[/bold yellow] binding to a non-localhost address exposes "
            "the admin interface on the network. It has no authentication; only do this on a "
            "trusted network."
        )
    if not no_browser:
        console.print("Opening browser...")
        _open_browser_when_ready(url)
    console.print("Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, log_level="warning")


def _open_browser_when_ready(url: str) -> None:
    """Open the browser shortly after the server has had time to bind.

    Runs on a daemon thread so it never blocks the (blocking)
    ``uvicorn.run`` call below it, and any failure to launch a browser
    (headless host, no ``$DISPLAY``) is swallowed -- the URL is already
    printed for the user to open manually.
    """

    def _launch() -> None:
        time.sleep(1.0)
        with contextlib.suppress(Exception):  # headless/no-browser is fine
            webbrowser.open(url)

    threading.Thread(target=_launch, name="ragmonk-ui-browser", daemon=True).start()
