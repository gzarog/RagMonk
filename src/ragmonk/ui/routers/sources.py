"""Source administration routes (Admin UI plan §5)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from ragmonk.core.errors import RagMonkError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import source_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


def _split_patterns(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


@router.get("/sources")
def list_sources(
    request: Request, ctx: AppContext = Depends(get_ctx), error: str | None = None
) -> Response:
    return render(
        request,
        "sources/list.html",
        "sources",
        sources=source_service.list_sources(ctx),
        error=error,
    )


@router.get("/sources/{source_id}")
def source_detail(source_id: str, request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        detail = source_service.source_detail(ctx, source_id)
    except RagMonkError as exc:
        return hx_redirect(f"/sources?error={exc}")
    return render(request, "sources/detail.html", "sources", source=detail)


@router.post("/sources")
def add_source(
    request: Request,
    path: Annotated[str, Form()],
    include_patterns: Annotated[str, Form()] = "",
    exclude_patterns: Annotated[str, Form()] = "",
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    try:
        source_service.add_source(
            ctx,
            path,
            include_patterns=_split_patterns(include_patterns),
            exclude_patterns=_split_patterns(exclude_patterns),
        )
    except RagMonkError as exc:
        return hx_redirect(f"/sources?error={exc}")
    return hx_redirect("/sources")


@router.post("/sources/{source_id}/enable")
def enable_source(source_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        source_service.set_enabled(ctx, source_id, True)
    except RagMonkError as exc:
        return hx_redirect(f"/sources?error={exc}")
    return hx_redirect("/sources")


@router.post("/sources/{source_id}/disable")
def disable_source(source_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        source_service.set_enabled(ctx, source_id, False)
    except RagMonkError as exc:
        return hx_redirect(f"/sources?error={exc}")
    return hx_redirect("/sources")


@router.post("/sources/{source_id}/remove")
def remove_source(source_id: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    # Destructive (§5.4, §14.4): the template requires an explicit
    # confirmation click before this POST is issued.
    try:
        source_service.remove_source(ctx, source_id)
    except RagMonkError as exc:
        return hx_redirect(f"/sources?error={exc}")
    return hx_redirect("/sources")
