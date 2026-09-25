"""Source administration operations shared by the CLI and the admin UI.

Admin UI plan, Phase 2 (§5) and Phase 12 (§15): the UI must not
re-implement source validation, the daemon-safety guard, or the
"delete derived data but never the originals" contract -- it reuses
:class:`ragmonk.sources.registry.SourceRegistry` exactly as the
``ragmonk source`` CLI does. This module adds only the *aggregation*
the UI needs on top of that registry (per-source counts + metrics for
the list and detail screens), so both adapters ask the same questions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Source
from ragmonk.sources.registry import SourceRegistry, SourceRemoval
from ragmonk.storage.repositories import (
    documents_repo,
    entities_repo,
    errors_repo,
    files_repo,
    jobs_repo,
    relationships_repo,
)


def _registry(ctx: AppContext) -> SourceRegistry:
    return SourceRegistry(ctx.sources_conn, home=ctx.home)


def _summarize(ctx: AppContext, source: Source) -> dict[str, Any]:
    project_id = paths.project_id_for_path(Path(source.path))
    # control_plane=True: mirrors ``service/status_service.py``'s
    # ``indexing_overview`` (Phase 8) -- the per-source registry/job-queue/
    # file-status numbers here are local control-plane state, deliberately
    # kept local regardless of storage.mode.
    conn = ctx.project_conn(project_id, control_plane=True)
    counts = files_repo.count_by_status(conn, source.id)
    return {
        "id": source.id,
        "path": source.path,
        "type": source.source_type.value,
        "enabled": source.enabled,
        "status": source.status.value,
        "include_patterns": source.include_patterns,
        "exclude_patterns": source.exclude_patterns,
        "last_scan_at": source.last_scan_at,
        "last_error": source.last_error,
        "counts": counts,
        "indexed": counts.get("indexed", 0),
        "failed": counts.get("failed", 0),
        "queued": counts.get("queued", 0),
        "queue_depth": jobs_repo.queue_depth(conn),
    }


def list_sources(ctx: AppContext) -> list[dict[str, Any]]:
    return [_summarize(ctx, source) for source in _registry(ctx).list()]


def source_detail(ctx: AppContext, source_id: str) -> dict[str, Any]:
    registry = _registry(ctx)
    source = registry.get(source_id)
    detail = _summarize(ctx, source)

    project_id = paths.project_id_for_path(Path(source.path))
    # control_plane=True: see the comment in ``_summarize`` above -- these
    # metrics mirror the same local-control-plane numbers ``status_service``
    # already documents as deliberately local regardless of storage.mode.
    conn = ctx.project_conn(project_id, control_plane=True)
    detail["metrics"] = {
        "symbols_created": entities_repo.count_all(conn),
        "relationships_created": relationships_repo.count_all(conn),
        "documents_processed": documents_repo.count_all(conn),
        "database_size_bytes": _file_size(paths.project_db_path(project_id, ctx.home)),
    }
    detail["errors"] = [
        {
            "path": record.path,
            "error_code": record.error_code,
            "error_message": record.error_message,
            "occurred_at": record.occurred_at,
        }
        for record in errors_repo.list_for_source(conn, source.id)[:25]
    ]
    return detail


def add_source(
    ctx: AppContext,
    raw_path: str,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> Source:
    return _registry(ctx).add(
        raw_path,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )


def set_enabled(ctx: AppContext, source_id: str, enabled: bool) -> Source:
    return _registry(ctx).set_enabled(source_id, enabled)


def remove_source(ctx: AppContext, source_id: str) -> SourceRemoval:
    registry = _registry(ctx)
    source = registry.get(source_id)
    # The long-lived UI AppContext caches a project connection the moment a
    # source is listed or summarized (``ctx.project_conn`` in
    # ``_summarize``). On Windows an open SQLite handle keeps ``knowledge.db``
    # locked, so ``registry.remove``'s ``shutil.rmtree`` of the project dir
    # would fail (POSIX lets you unlink an open file, Windows does not).
    # Drop the cached handle first -- the same thing ``ops/rebuild`` does
    # before wiping a project (``AppContext.close_project_conn``). The CLI
    # never needs this because it bootstraps a fresh context per command.
    project_id = paths.project_id_for_path(Path(source.path))
    ctx.close_project_conn(project_id)
    # Storage backend abstraction plan, Phase 8: purge the source's
    # searchable knowledge from the real server backend too (every
    # generation, per ``clear_source``'s own contract) before the source
    # stops being registered -- see ``ragmonk.cli.source.remove``'s
    # identical call for the full rationale. Control-plane bookkeeping
    # (the ``sources`` registry row/project dir, below) stays local
    # regardless of storage mode.
    if ctx.config.storage.mode == "server":
        ctx.backend().clear_source(source_id)
    return registry.remove(source_id)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
