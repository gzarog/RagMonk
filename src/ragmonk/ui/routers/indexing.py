"""Indexing administration routes (Admin UI plan §6)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import index_service
from ragmonk.service.index_service import INDEXER
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


@router.get("/indexing")
def indexing_page(
    request: Request, ctx: AppContext = Depends(get_ctx), error: str | None = None
) -> Response:
    return render(
        request,
        "indexing/index.html",
        "indexing",
        overview=index_service.indexing_overview(ctx),
        error=error,
    )


@router.get("/indexing/failed")
def failed_files(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    return render(
        request,
        "indexing/failed.html",
        "indexing",
        failed=index_service.failed_files(ctx),
    )


@router.post("/indexing/start")
def start_indexing(source_id: Annotated[str, Form()] = "") -> Response:
    started = INDEXER.start(source_id=source_id or None)
    if not started:
        return hx_redirect("/indexing?error=an+indexing+run+is+already+in+progress")
    return hx_redirect("/indexing")


@router.post("/indexing/rebuild")
def rebuild(
    source_id: Annotated[str, Form()] = "",
    fresh: Annotated[str, Form()] = "",
) -> Response:
    # Destructive (§6.3, §14.4): confirmation is enforced in the template.
    started = INDEXER.rebuild(source_id=source_id or None, fresh=bool(fresh))
    if not started:
        return hx_redirect("/indexing?error=an+indexing+run+is+already+in+progress")
    return hx_redirect("/indexing")
