"""AI provider routes (Admin UI plan §10)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import ai_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/ai")
def ai_page(
    request: Request, ctx: AppContext = Depends(get_ctx), test: str | None = None
) -> Response:
    result = None
    if test:
        result = ai_service.test_provider(ctx, test)
    return render(
        request,
        "ai/index.html",
        "ai",
        overview=ai_service.provider_overview(ctx),
        test_result=result,
    )


@router.post("/ai/test")
def test_provider(provider: Annotated[str, Form()], ctx: AppContext = Depends(get_ctx)) -> Response:
    from ragmonk.ui.templating import hx_redirect

    return hx_redirect(f"/ai?test={provider}")
