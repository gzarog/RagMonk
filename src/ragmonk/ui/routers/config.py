"""Configuration routes (Admin UI plan §9)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import config_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


@router.get("/config")
def config_page(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    saved: str | None = None,
    error: str | None = None,
) -> Response:
    return render(
        request,
        "config/index.html",
        "config",
        sections=config_service.describe_config(ctx),
        saved=saved,
        error=error,
    )


@router.post("/config")
async def save_config(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    form = await request.form()
    updates = {key: str(value) for key, value in form.items() if key != "csrf_token"}
    try:
        config_service.apply_updates(ctx, updates)
    except Exception as exc:  # noqa: BLE001 - validation errors surfaced to the user
        return hx_redirect(f"/config?error={exc}")
    return hx_redirect("/config?saved=1")
