"""Reusable status/dashboard aggregation shared by the CLI and the admin UI.

Admin UI plan, Phase 1 (§4.6) and Phase 12 (§15): the numbers the
``ragmonk status`` command prints and the numbers the UI dashboard shows
must come from *one* place, not two hand-kept-in-sync copies. This module
owns that aggregation. ``ragmonk.cli.status`` calls :func:`collect_status`
and renders it as a Rich table; the UI dashboard router calls the same
function and renders it as HTML. Neither re-implements the counting.

Everything here reads the local SQLite databases only -- no network, no
tokenizer load beyond the cheap identity lookup -- so it is safe to call
on every dashboard render.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import health, pid
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import (
    documents_repo,
    entities_repo,
    errors_repo,
    files_repo,
    jobs_repo,
    relationships_repo,
)
from ragmonk.tokenization import diagnostics


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def collect_status(ctx: AppContext) -> dict[str, Any]:
    """Aggregate per-source and total indexing metrics for a runtime.

    Shared with Phase 6's ``ragmonk_status`` MCP tool and the admin UI
    dashboard. Metrics here are real, cheaply-obtainable numbers this
    codebase already tracks -- file/job counts, entity/relationship
    counts, document counts, database file size on disk. Deliberately
    excluded: query-latency style metrics -- nothing in this codebase
    times a query today, and inventing one only for a metrics field
    would be a fabricated number, not a real one.
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = registry.list()

    per_source: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    total_queue_depth = 0
    total_symbols = 0
    total_relationships = 0
    total_documents = 0
    total_db_bytes = _file_size(paths.sources_db_path(ctx.home))

    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        conn = ctx.project_conn(project_id)
        counts = files_repo.count_by_status(conn, source.id)
        depth = jobs_repo.queue_depth(conn)
        total_queue_depth += depth
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value

        symbols = entities_repo.count_all(conn)
        relationships = relationships_repo.count_all(conn)
        documents = documents_repo.count_all(conn)
        db_bytes = _file_size(paths.project_db_path(project_id, ctx.home))
        total_symbols += symbols
        total_relationships += relationships
        total_documents += documents
        total_db_bytes += db_bytes

        per_source.append(
            {
                "id": source.id,
                "path": source.path,
                "type": source.source_type.value,
                "enabled": source.enabled,
                "status": source.status.value,
                "counts": counts,
                "queue_depth": depth,
                "last_scan_at": source.last_scan_at,
                "last_error": source.last_error,
                "metrics": {
                    "symbols_created": symbols,
                    "relationships_created": relationships,
                    "documents_processed": documents,
                    "database_size_bytes": db_bytes,
                },
            }
        )

    tokenizer = diagnostics.tokenizer_identity()
    tokenizer["chunk_ceiling"] = ctx.config.documents.chunking.resolved_max_tokens

    return {
        "sources": per_source,
        "tokenizer": tokenizer,
        "totals": {
            "by_status": totals,
            "queue_depth": total_queue_depth,
            "metrics": {
                "files_discovered": sum(totals.values()),
                "files_indexed": totals.get("indexed", 0),
                "files_failed": totals.get("failed", 0),
                "symbols_created": total_symbols,
                "relationships_created": total_relationships,
                "documents_processed": total_documents,
                "index_queue_depth": total_queue_depth,
                "database_size_bytes": total_db_bytes,
            },
        },
    }


def daemon_snapshot(ctx: AppContext) -> dict[str, Any]:
    """Current daemon run-state (running/pid/started/last activity).

    Mirrors ``ragmonk daemon status`` so the dashboard and the Daemon
    page can show live state without duplicating the read.
    """
    info = pid.running_daemon(ctx.home)
    snapshot = health.read_health(ctx.home)
    return {
        "running": info is not None,
        "pid": info.pid if info else None,
        "started_at": info.started_at if info else None,
        "last_reconciliation_at": snapshot.last_reconciliation_at if snapshot else None,
        "sources": [
            {
                "source_id": s.source_id,
                "path": s.path,
                "source_type": s.source_type,
                "online": s.online,
                "last_pass_at": s.last_pass_at,
            }
            for s in (snapshot.sources if snapshot else [])
        ],
    }


def recent_errors(ctx: AppContext, *, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent indexing errors across all sources, newest first.

    Feeds the dashboard's "latest indexing errors" panel (§4.6) and the
    indexing page's recent-errors list (§6.1).
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    collected: list[dict[str, Any]] = []
    for source in registry.list():
        project_id = paths.project_id_for_path(Path(source.path))
        conn = ctx.project_conn(project_id)
        for record in errors_repo.list_for_source(conn, source.id):
            collected.append(
                {
                    "source_id": source.id,
                    "path": record.path,
                    "error_code": record.error_code,
                    "error_message": record.error_message,
                    "occurred_at": record.occurred_at,
                }
            )
    collected.sort(key=lambda r: r["occurred_at"] or "", reverse=True)
    return collected[:limit]
