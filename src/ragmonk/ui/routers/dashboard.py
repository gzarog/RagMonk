"""Dashboard route (Admin UI plan §4.6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import status_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    status = status_service.collect_status(ctx)
    return render(
        request,
        "dashboard.html",
        "dashboard",
        status=status,
        daemon=status_service.daemon_snapshot(ctx),
        errors=status_service.recent_errors(ctx, limit=10),
    )
