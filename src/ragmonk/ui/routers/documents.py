"""Documents / indexed-content routes (Admin UI plan §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import document_service, source_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


@router.get("/documents")
def list_documents(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    source_id: str | None = None,
    q: str | None = None,
    fmt: str | None = None,
    page: int = 1,
) -> Response:
    result = document_service.list_documents(
        ctx, source_id=source_id or None, query=q or None, fmt=fmt or None, page=page
    )
    return render(
        request,
        "documents/list.html",
        "documents",
        result=result,
        sources=source_service.list_sources(ctx),
        filters={"source_id": source_id or "", "q": q or "", "fmt": fmt or ""},
    )


@router.get("/documents/{source_id}/{document_id}")
def document_detail(
    source_id: str, document_id: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    try:
        document = document_service.document_detail(ctx, source_id, document_id)
    except LookupError as exc:
        return hx_redirect(f"/documents?error={exc}")
    return render(request, "documents/detail.html", "documents", document=document)
