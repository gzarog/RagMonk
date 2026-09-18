"""FastAPI application factory for the RagMonk admin UI.

Admin UI plan, Phase 1 (§4.4): construct the app, register routers, mount
static files, configure templates, initialize RagMonk dependencies, expose
``/health``, and manage the startup/shutdown lifecycle. An app *factory*
(not a global singleton) is used so tests can build isolated instances
with their own ``home`` (§4.4).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from ragmonk.core.lifecycle import AppContext
from ragmonk.ui import events
from ragmonk.ui.routers import (
    ai,
    backups,
    config,
    daemon,
    dashboard,
    documents,
    health,
    indexing,
    knowledge,
    logs,
    search,
    sources,
    system,
)
from ragmonk.ui.security import CSRFMiddleware, HostValidationMiddleware

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *,
    home: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    **bootstrap_kwargs: Any,
) -> FastAPI:
    import threading

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        # §4.5: reuse AppContext.bootstrap(); §4.5 shutdown: close it.
        ctx = AppContext.bootstrap(home=home, **bootstrap_kwargs)
        app.state.ctx = ctx
        app.state.db_lock = threading.Lock()
        try:
            yield
        finally:
            ctx.close()

    app = FastAPI(
        title="RagMonk Admin UI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # §14.3 host validation (outermost) then §14.2 CSRF. Middleware added
    # later runs first, so add CSRF first and host validation last.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(HostValidationMiddleware, host=host, port=port)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health", include_in_schema=False)
    async def health_probe() -> dict[str, str]:
        """Liveness probe (§4.4). Distinct from the Health *page*
        (``/health/`` router) -- this is a plain JSON heartbeat."""
        return {"status": "ok"}

    @app.get("/events/indexing", include_in_schema=False)
    async def indexing_events(request: Request) -> StreamingResponse:
        return await events.indexing_event_stream(request)

    app.include_router(dashboard.router)
    app.include_router(sources.router)
    app.include_router(indexing.router)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(knowledge.router)
    app.include_router(ai.router)
    app.include_router(config.router)
    app.include_router(daemon.router)
    app.include_router(health.router)
    app.include_router(backups.router)
    app.include_router(logs.router)
    app.include_router(system.router)

    return app
