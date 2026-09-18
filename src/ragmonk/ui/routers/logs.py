"""Log viewer route (Admin UI plan §13.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import logs_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/logs")
def logs_page(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    level: str | None = None,
    component: str | None = None,
    q: str | None = None,
    errors_only: bool = False,
) -> Response:
    data = logs_service.read_logs(
        ctx,
        level=level or None,
        component=component or None,
        query=q or None,
        errors_only=errors_only,
    )
    return render(
        request,
        "logs/index.html",
        "logs",
        data=data,
        filters={
            "level": level or "",
            "component": component or "",
            "q": q or "",
            "errors_only": errors_only,
        },
    )
