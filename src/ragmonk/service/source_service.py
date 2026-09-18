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
    conn = ctx.project_conn(project_id)
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
    conn = ctx.project_conn(project_id)
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
    return _registry(ctx).remove(source_id)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
