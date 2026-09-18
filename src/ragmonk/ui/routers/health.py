"""Health / doctor page (Admin UI plan §12.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import health_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/health/")
def health_page(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    return render(request, "health/index.html", "health", report=health_service.health_report(ctx))
