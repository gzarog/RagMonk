"""Daemon / runtime routes (Admin UI plan §11)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.errors import RagMonkError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import daemon_service, status_service
from ragmonk.ui.dependencies import get_ctx, get_ctx_unlocked
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


@router.get("/daemon")
def daemon_page(
    request: Request, ctx: AppContext = Depends(get_ctx), error: str | None = None
) -> Response:
    return render(
        request,
        "daemon/index.html",
        "daemon",
        daemon=status_service.daemon_snapshot(ctx),
        error=error,
    )


@router.post("/daemon/start")
def start(ctx: AppContext = Depends(get_ctx_unlocked)) -> Response:
    try:
        daemon_service.start(ctx)
    except RagMonkError as exc:
        return hx_redirect(f"/daemon?error={exc}")
    return hx_redirect("/daemon")


@router.post("/daemon/stop")
def stop(ctx: AppContext = Depends(get_ctx_unlocked)) -> Response:
    try:
        daemon_service.stop(ctx)
    except RagMonkError as exc:
        return hx_redirect(f"/daemon?error={exc}")
    return hx_redirect("/daemon")


@router.post("/daemon/restart")
def restart(ctx: AppContext = Depends(get_ctx_unlocked)) -> Response:
    try:
        daemon_service.restart(ctx)
    except RagMonkError as exc:
        return hx_redirect(f"/daemon?error={exc}")
    return hx_redirect("/daemon")
