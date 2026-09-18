"""RagMonk built-in local administration web interface.

Admin UI plan: ``ragmonk ui`` serves this FastAPI application on
``127.0.0.1`` by default. It reuses the same RagMonk application services
the CLI and MCP server use (``ragmonk.service.*``) rather than shelling
out to CLI commands, and ships its templates and vendored static assets
inside the wheel so it works offline with no Node.js.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["create_app"]


def create_app(**kwargs: Any) -> FastAPI:
    """Lazy re-export of the app factory.

    Imported lazily so ``import ragmonk.ui`` does not pull FastAPI in
    until an app is actually built (keeps ``ragmonk --help`` and every
    non-UI command free of the web stack).
    """
    from ragmonk.ui.app import create_app as _create_app

    return _create_app(**kwargs)
