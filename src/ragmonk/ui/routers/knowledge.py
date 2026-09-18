"""Code-knowledge routes (Admin UI plan §8.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import knowledge_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import render

router = APIRouter()


@router.get("/knowledge")
def knowledge_home(
    request: Request, ctx: AppContext = Depends(get_ctx), q: str | None = None
) -> Response:
    return render(
        request,
        "knowledge/index.html",
        "knowledge",
        query=q or "",
        symbols=knowledge_service.list_symbols(ctx, query=q or None),
    )


@router.get("/knowledge/symbol")
def symbol_detail(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    name: str = "",
    relation: str = "callers",
) -> Response:
    data = None
    if name:
        fn = {
            "callers": knowledge_service.callers,
            "callees": knowledge_service.callees,
            "references": knowledge_service.references,
            "impact": knowledge_service.impact,
        }.get(relation, knowledge_service.callers)
        data = fn(ctx, name)
    return render(
        request,
        "knowledge/symbol.html",
        "knowledge",
        name=name,
        relation=relation,
        data=data,
    )
