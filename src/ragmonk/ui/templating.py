"""Shared Jinja2 environment and render helper for the UI.

Centralizes the ``Jinja2Templates`` instance and the common template
context (navigation, RagMonk version, CSRF token) so every router renders
the same chrome without repeating it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from ragmonk import __version__
from ragmonk.ui.security import current_csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Admin UI plan §4.7 / §16: the left-hand navigation. ``(slug, label,
# url)`` -- every screen listed in the plan appears; all are implemented
# in this build.
NAV_ITEMS: list[tuple[str, str, str]] = [
    ("dashboard", "Dashboard", "/"),
    ("sources", "Sources", "/sources"),
    ("indexing", "Indexing", "/indexing"),
    ("documents", "Documents", "/documents"),
    ("search", "Search", "/search"),
    ("knowledge", "Knowledge", "/knowledge"),
    ("ai", "AI", "/ai"),
    ("config", "Configuration", "/config"),
    ("daemon", "Daemon", "/daemon"),
    ("health", "Health", "/health/"),
    ("backups", "Backups", "/backups"),
    ("logs", "Logs", "/logs"),
    ("system", "System", "/system"),
]

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, template: str, active: str, **context: Any) -> Response:
    """Render ``template`` with the standard chrome context merged in."""
    base = {
        "request": request,
        "nav_items": NAV_ITEMS,
        "active": active,
        "ragmonk_version": __version__,
        "csrf_token": current_csrf_token(request),
    }
    base.update(context)
    return templates.TemplateResponse(request=request, name=template, context=base)


def hx_redirect(url: str) -> Response:
    """Tell HTMX to navigate the browser to ``url`` after a mutation.

    Mutating actions are issued via HTMX (so the CSRF header rides along);
    on success they return this 204 with an ``HX-Redirect`` header, which
    HTMX turns into a full client-side navigation to the refreshed page.
    """
    return Response(status_code=204, headers={"HX-Redirect": url})

