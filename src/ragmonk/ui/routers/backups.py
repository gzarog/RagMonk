"""Backup / restore routes (Admin UI plan §12.1/§12.2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import FileResponse, Response

from ragmonk.core.lifecycle import AppContext
from ragmonk.service import backup_service
from ragmonk.ui.dependencies import get_ctx
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()


@router.get("/backups")
def backups_page(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    saved: str | None = None,
    error: str | None = None,
) -> Response:
    return render(
        request,
        "backups/index.html",
        "backups",
        backups=backup_service.list_backups(ctx),
        update=backup_service.update_status(ctx),
        saved=saved,
        error=error,
    )


@router.post("/backups")
def create_backup(ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        backup_service.create(ctx)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        return hx_redirect(f"/backups?error={exc}")
    return hx_redirect("/backups?saved=1")


@router.post("/backups/restore")
def restore_backup(name: Annotated[str, Form()], ctx: AppContext = Depends(get_ctx)) -> Response:
    # Destructive (§12.2, §14.4): confirmation enforced in the template.
    try:
        backup_service.restore(ctx, name)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        return hx_redirect(f"/backups?error={exc}")
    return hx_redirect("/backups?saved=restored")


@router.get("/backups/download/{name}")
def download_backup(name: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        path = backup_service.backup_path(ctx, name)
    except LookupError as exc:
        return hx_redirect(f"/backups?error={exc}")
    return FileResponse(path, filename=path.name, media_type="application/gzip")
