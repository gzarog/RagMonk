"""System / updates page (Admin UI plan §12.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import backup_service, status_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/system")
def system_page(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    status = status_service.collect_status(ctx)
    return render(
        request,
        "system/index.html",
        "system",
        update=backup_service.update_status(ctx),
        tokenizer=status["tokenizer"],
        home=str(ctx.home),
    )
