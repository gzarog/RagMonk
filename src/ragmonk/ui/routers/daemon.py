"""Daemon / runtime routes (Admin UI plan §11)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from ragmonk.core import paths
from ragmonk.core.errors import RagMonkError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import daemon_service, status_service
from ragmonk.ui.dependencies import get_ctx, get_ctx_unlocked
from ragmonk.ui.templating import hx_redirect, render

router = APIRouter()

_MAX_LOG_LINES = 100


def _read_log_tail(home: Path, max_lines: int = _MAX_LOG_LINES) -> list[str]:
    log_path = paths.logs_dir(home) / "daemon.out.log"
    if not log_path.is_file():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def _format_uptime(started_at: str | None) -> str:
    if not started_at:
        return ""
    try:
        started = datetime.fromisoformat(started_at)
        seconds = int((datetime.now(UTC) - started).total_seconds())
    except (ValueError, TypeError):
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


@router.get("/daemon")
def daemon_page(
    request: Request, ctx: AppContext = Depends(get_ctx), error: str | None = None
) -> Response:
    daemon = status_service.daemon_snapshot(ctx)
    return render(
        request,
        "daemon/index.html",
        "daemon",
        daemon=daemon,
        uptime=_format_uptime(daemon.get("started_at")),
        log_lines=_read_log_tail(ctx.home, max_lines=50),
        error=error,
    )


@router.get("/daemon/panel")
def daemon_panel(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    """HTMX partial: returns just the daemon status panel for live polling."""
    daemon = status_service.daemon_snapshot(ctx)
    return render(
        request,
        "daemon/_panel.html",
        "daemon",
        daemon=daemon,
        uptime=_format_uptime(daemon.get("started_at")),
    )


@router.get("/daemon/logs")
def daemon_logs(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    """HTMX partial: returns the log tail for live polling."""
    return render(
        request,
        "daemon/_logs.html",
        "daemon",
        log_lines=_read_log_tail(ctx.home, max_lines=50),
    )


@router.get("/daemon/api/status")
def daemon_status_api(ctx: AppContext = Depends(get_ctx)) -> JSONResponse:
    daemon = status_service.daemon_snapshot(ctx)
    daemon["uptime"] = _format_uptime(daemon.get("started_at"))
    return JSONResponse(daemon)


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
