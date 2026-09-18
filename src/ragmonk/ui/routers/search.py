"""Search route (Admin UI plan §8.1/§8.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import search_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/search")
def search_page(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    q: str | None = None,
    mode: str = "lexical",
    limit: int = 20,
) -> Response:
    results = None
    if q:
        results = search_service.run_search(ctx, q, mode=mode, limit=limit)
    return render(
        request,
        "search/index.html",
        "search",
        query=q or "",
        mode=mode,
        limit=limit,
        results=results,
    )
