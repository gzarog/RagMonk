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

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext, inspect_lock
from ragmonk.core.models import SourceStatus
from ragmonk.service import health, pid, progress
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


def _backend_kind(ctx: AppContext) -> str:
    """``"local"``, ``"server/opensearch"`` or ``"server/elasticsearch"`` --
    the same three-way distinction ``ragmonk doctor``'s server section
    reports, for ``ragmonk status``/the admin UI dashboard's own use
    (Storage backend abstraction plan, Phase 8).
    """
    if ctx.config.storage.mode != "server":
        return "local"
    return f"server/{ctx.config.storage.server.engine}"


def collect_status(ctx: AppContext) -> dict[str, Any]:
    """Aggregate per-source and total indexing metrics for a runtime.

    Shared with Phase 6's ``ragmonk_status`` MCP tool and the admin UI
    dashboard. Metrics here are real, cheaply-obtainable numbers this
    codebase already tracks -- file/job counts, entity/relationship
    counts, document counts, database file size on disk. Deliberately
    excluded: query-latency style metrics -- nothing in this codebase
    times a query today, and inventing one only for a metrics field
    would be a fabricated number, not a real one.

    In server mode (``storage.mode == "server"``), also reports the
    backend's own real counts (``ctx.backend().count_stats()``) alongside
    the per-source local numbers above -- the per-source table itself
    still reflects the local control-plane registry/job-queue state
    (sources, scan status, queue depth), which stays local regardless of
    storage mode; only the searchable-knowledge counts additionally come
    from the real server backend here. Local mode's own output is
    unchanged (Storage backend abstraction plan, Phase 8).
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = registry.list()

    indexer = indexer_state(ctx)
    per_source: list[dict[str, Any]] = []
    queue_stats: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    total_queue_depth = 0
    total_symbols = 0
    total_relationships = 0
    total_documents = 0
    total_db_bytes = _file_size(paths.sources_db_path(ctx.home))

    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        # control_plane=True: this function's docstring above already
        # documents the deliberate design (Phase 8) -- the per-source
        # table stays local control-plane state (registry/job-queue/file
        # status) regardless of storage.mode; the real searchable-knowledge
        # counts are read separately below via ``ctx.backend().count_stats()``
        # in server mode.
        conn = ctx.project_conn(project_id, control_plane=True)
        counts = files_repo.count_by_status(conn, source.id)
        depth = jobs_repo.queue_depth(conn)
        queue = jobs_repo.queue_stats(conn, source.id)
        queue_stats.append(queue)
        total_queue_depth += depth
        last_error_detail = _latest_error(conn, source.id)
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
                # "Last Scan" = last completed scan attempt, not full
                # indexing success; see ``Source.last_scan_at``.
                "last_scan_at": source.last_scan_at,
                "last_error": source.last_error,
                # Status observability V1 (additive): reachability of the
                # source root, kept separate from what indexing is doing.
                "access_state": _access_state(source.status, source.enabled),
                "index_state": derive_index_state(
                    access_state=_access_state(source.status, source.enabled),
                    queue=queue,
                    failed_files=counts.get("failed", 0),
                    last_error=source.last_error,
                    last_scan_at=source.last_scan_at,
                    indexer=indexer,
                    source_id=source.id,
                ),
                "queue": queue,
                "last_activity_at": _source_last_activity(indexer, source.id, source.last_scan_at),
                "last_error_detail": last_error_detail,
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

    backend_kind = _backend_kind(ctx)
    backend_info: dict[str, Any] = {"type": backend_kind}
    if ctx.config.storage.mode == "server":
        # Real server-side counts, not the local-only numbers above --
        # never silently reused/estimated from the local control plane.
        # A backend that cannot be reached surfaces as an explicit error
        # string rather than a fabricated zero/omitted field.
        try:
            stats = ctx.backend().count_stats()
            backend_info["counts"] = {
                "files": stats.files,
                "entities": stats.entities,
                "document_units": stats.document_units,
                "embeddings": stats.embeddings,
            }
        except Exception as exc:
            backend_info["error"] = f"server unreachable: {exc}"

    queue_totals = _merge_queue_stats(queue_stats)
    errors = recent_errors(ctx, limit=_DEFAULT_RECENT_ERRORS)
    problems = derive_problems(per_source, indexer, backend_info, queue_totals)
    health_status = derive_health(problems)
    sources_with_errors = sum(1 for row in per_source if _source_has_errors(row))

    return {
        "health": {
            "status": health_status,
            "problem_count": len(problems),
        },
        "indexer": indexer,
        "queue": queue_totals,
        "recent_errors": errors,
        "recent_error_count": len(errors),
        "sources_with_errors": sources_with_errors,
        "problems": problems,
        "sources": per_source,
        "tokenizer": tokenizer,
        "backend": backend_info,
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
        # control_plane=True: indexing-error bookkeeping is local
        # control-plane state (the record of what failed during a local
        # scan/process pass), not searchable knowledge data -- see the
        # docstring note in ``indexing_overview`` above for the same
        # per-source-stays-local design (Phase 8).
        conn = ctx.project_conn(project_id, control_plane=True)
        try:
            records = errors_repo.list_for_source(conn, source.id, limit=limit)
        except (sqlite3.Error, ValueError):
            # One source's malformed/partial error history must never
            # break status for every other source.
            continue
        for record in records:
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


# -- Status observability V1 -------------------------------------------
#
# Everything below derives the "is indexing running / progressing /
# stalled / failing?" answers from three independent, cheap, read-only
# signals: the ``index.lock`` OS lock (authoritative for "someone is
# indexing"), the progress snapshot (``service/progress.py``: which
# source/stage, and a heartbeat), and the local job queue. Queue depth
# alone never implies activity or a stall.

_DEFAULT_RECENT_ERRORS = 10

# Deterministic precedence for ``index_state`` (first match wins).
INDEX_STATE_PRECEDENCE = (
    "offline",
    "stalled",
    "indexing",
    "retrying",
    "errors",
    "waiting",
    "completed",
    "idle",
)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_seconds(value: str | None, now: datetime) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return round(max(0.0, (now - parsed).total_seconds()), 1)


def _access_state(status: SourceStatus, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    return "offline" if status is SourceStatus.OFFLINE else "online"


def indexer_state(ctx: AppContext, *, now: datetime | None = None) -> dict[str, Any]:
    """Merge lock ownership and the progress snapshot into one verdict.

    ``state`` is one of:

    * ``running`` -- the index lock is held, or a live process's progress
      snapshot says a run is in flight (``ragmonk index`` releases the
      lock briefly between sources), with a fresh heartbeat.
    * ``stalled`` -- as ``running``, but the heartbeat is older than
      ``indexing.status_stall_threshold_seconds``.
    * ``crashed`` -- the snapshot says running, but the lock is free and
      its process is gone: historical information, not active work.
    * ``idle`` -- nothing is indexing.

    Never blocks on, releases or otherwise disturbs the lock.
    """
    now = now or datetime.now(UTC)
    threshold = float(ctx.config.indexing.status_stall_threshold_seconds)
    lock = inspect_lock(paths.locks_dir(ctx.home) / "index.lock")
    owner = lock.owner or {}
    snapshot = progress.read_progress(ctx.home)

    info: dict[str, Any] = {
        "state": "idle",
        "lock_state": lock.state,
        "pid": None,
        "operation": None,
        "hostname": owner.get("hostname"),
        "lock_source_id": owner.get("source_id"),
        "acquired_at": owner.get("acquired_at"),
        "running_for_seconds": None,
        "current_source_id": None,
        "source_position": None,
        "source_total": None,
        "stage": None,
        "started_at": None,
        "last_activity_at": None,
        "last_activity_age_seconds": None,
        "stall_threshold_seconds": threshold,
        "stall_reason": None,
        "run": None,
        "last_run": None,
    }
    if lock.state == "held":
        info["pid"] = owner.get("pid")
        info["operation"] = owner.get("operation")

    snapshot_live = False
    if snapshot is not None:
        run = {
            "operation": snapshot.operation,
            "outcome": snapshot.outcome,
            "pid": snapshot.pid,
            "started_at": snapshot.started_at,
            "updated_at": snapshot.updated_at,
            "completed_at": snapshot.completed_at,
            "scanned": snapshot.scanned,
            "queued": snapshot.queued,
            "retry": snapshot.retry,
            "indexed": snapshot.indexed,
            "failed": snapshot.failed,
            "error": snapshot.error,
        }
        if snapshot.running:
            pid_alive = snapshot.pid is not None and pid.is_process_alive(snapshot.pid)
            # A "running" snapshot is live when its writer still holds the
            # lock (same pid) or its process is still alive.
            same_owner = lock.state == "held" and owner.get("pid") in (None, snapshot.pid)
            snapshot_live = same_owner or pid_alive
            if snapshot_live:
                info["run"] = run
            else:
                run["outcome"] = "crashed"
                info["last_run"] = run
        else:
            info["last_run"] = run

    if snapshot_live and snapshot is not None:
        info["pid"] = info["pid"] or snapshot.pid
        info["operation"] = info["operation"] or snapshot.operation
        info["current_source_id"] = snapshot.source_id
        info["source_position"] = snapshot.source_position
        info["source_total"] = snapshot.source_total
        info["stage"] = snapshot.stage
        info["started_at"] = snapshot.started_at
        info["last_activity_at"] = snapshot.updated_at
    elif lock.state == "held":
        # Lock held but no live snapshot (e.g. an older RagMonk version
        # indexing): running, source from the lock, heartbeat unknown.
        info["current_source_id"] = owner.get("source_id")
        info["started_at"] = owner.get("acquired_at")

    info["last_activity_age_seconds"] = _age_seconds(info["last_activity_at"], now)
    info["running_for_seconds"] = _age_seconds(info["acquired_at"] or info["started_at"], now)

    if lock.state == "held" or snapshot_live:
        age = info["last_activity_age_seconds"]
        if age is not None and age > threshold:
            info["state"] = "stalled"
            info["stall_reason"] = f"no progress heartbeat for {int(age)}s"
        else:
            info["state"] = "running"
    elif info["last_run"] is not None and info["last_run"]["outcome"] == "crashed":
        info["state"] = "crashed"
    return info


def derive_index_state(
    *,
    access_state: str,
    queue: dict[str, Any],
    failed_files: int,
    last_error: str | None,
    last_scan_at: str | None,
    indexer: dict[str, Any],
    source_id: str,
) -> str:
    """Per-source indexing state; first match in
    :data:`INDEX_STATE_PRECEDENCE` wins. ``access_state`` (root
    reachability) is an input, never a synonym: a reachable source with no
    active work is not ``indexing``.
    """
    is_current = indexer.get("current_source_id") == source_id
    if access_state == "offline":
        return "offline"
    if is_current and indexer.get("state") == "stalled":
        return "stalled"
    if is_current and indexer.get("state") == "running":
        return "indexing"
    if queue.get("retry", 0) > 0:
        return "retrying"
    if failed_files > 0 or queue.get("failed", 0) > 0 or last_error:
        return "errors"
    if queue.get("queued", 0) > 0 or queue.get("processing", 0) > 0:
        return "waiting"
    if last_scan_at:
        return "completed"
    return "idle"


def _source_last_activity(
    indexer: dict[str, Any], source_id: str, last_scan_at: str | None
) -> str | None:
    if indexer.get("current_source_id") == source_id and indexer.get("last_activity_at"):
        return str(indexer["last_activity_at"])
    return last_scan_at


def _latest_error(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    try:
        records = errors_repo.list_for_source(conn, source_id, limit=1)
    except (sqlite3.Error, ValueError):
        return None
    if not records:
        return None
    record = records[0]
    return {
        "path": record.path,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "occurred_at": record.occurred_at,
    }


def _merge_queue_stats(stats: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "queued": 0,
        "processing": 0,
        "retry": 0,
        "failed": 0,
        "completed": 0,
        "depth": 0,
        "oldest_pending_created_at": None,
        "oldest_processing_started_at": None,
        "next_retry_at": None,
        "max_attempt_count": 0,
        "latest_job_error": None,
    }
    for item in stats:
        if merged["latest_job_error"] is None and item.get("latest_job_error"):
            merged["latest_job_error"] = item["latest_job_error"]
        for key in ("queued", "processing", "retry", "failed", "completed", "depth"):
            merged[key] += item.get(key, 0)
        for key in ("oldest_pending_created_at", "oldest_processing_started_at", "next_retry_at"):
            value = item.get(key)
            if value and (merged[key] is None or value < merged[key]):
                merged[key] = value
        merged["max_attempt_count"] = max(
            merged["max_attempt_count"], item.get("max_attempt_count", 0)
        )
    return merged


def _source_has_errors(row: dict[str, Any]) -> bool:
    return bool(
        row.get("last_error")
        or row["counts"].get("failed", 0)
        or row.get("queue", {}).get("failed", 0)
        or row.get("queue", {}).get("retry", 0)
        or row.get("access_state") == "offline"
    )


def derive_problems(
    sources: list[dict[str, Any]],
    indexer: dict[str, Any],
    backend: dict[str, Any],
    queue: dict[str, Any],
) -> list[dict[str, Any]]:
    """Explain the health verdict. Severities: ``error`` (work cannot
    continue -> ``failed``), ``warning`` (``degraded``), ``info`` (no
    effect on health). Scopes follow the error model: ``file``,
    ``source``, ``backend``, ``runtime``.
    """
    problems: list[dict[str, Any]] = []

    def add(severity: str, scope: str, message: str, source_id: str | None = None) -> None:
        problems.append(
            {"severity": severity, "scope": scope, "source_id": source_id, "message": message}
        )

    if backend.get("error"):
        add("error", "backend", str(backend["error"]))

    if indexer.get("state") == "stalled":
        add(
            "error",
            "runtime",
            f"indexing stalled: {indexer.get('stall_reason')}",
            indexer.get("current_source_id"),
        )
    last_run = indexer.get("last_run") or {}
    if indexer.get("state") == "crashed":
        add(
            "error",
            "runtime",
            f"last {last_run.get('operation') or 'index'} run (pid {last_run.get('pid')}) "
            "exited without finishing",
        )
    elif last_run.get("outcome") == "failed" and indexer.get("state") == "idle":
        error = str(last_run.get("error") or "")
        severity = "warning" if error.startswith("KeyboardInterrupt") else "error"
        add(severity, "runtime", f"last {last_run.get('operation') or 'index'} run failed: {error}")

    enabled = [row for row in sources if row.get("enabled")]
    offline = [row for row in enabled if row.get("access_state") == "offline"]
    for row in offline:
        add(
            "warning",
            "source",
            f"source offline: {row.get('last_error') or 'unreachable'}",
            row["id"],
        )
    if enabled and len(offline) == len(enabled):
        add("error", "source", "all enabled sources are offline")

    for row in sources:
        if row.get("access_state") == "offline":
            continue
        if row.get("last_error"):
            add("warning", "source", str(row["last_error"]), row["id"])
        failed = row["counts"].get("failed", 0)
        if failed:
            add("warning", "file", f"{failed} file(s) failed", row["id"])
        retry = row.get("queue", {}).get("retry", 0)
        if retry:
            latest = row.get("queue", {}).get("latest_job_error") or {}
            detail = (
                f" (last: {latest.get('error_code')}: {latest.get('error_message')})"
                if latest
                else ""
            )
            add("warning", "file", f"{retry} job(s) awaiting retry{detail}", row["id"])

    if queue.get("depth", 0) and indexer.get("state") in ("idle", "crashed"):
        add(
            "info",
            "runtime",
            f"{queue['depth']} job(s) pending with no indexer running "
            "(run 'ragmonk index' or start the daemon)",
        )
    return problems


def derive_health(problems: list[dict[str, Any]]) -> str:
    severities = {problem["severity"] for problem in problems}
    if "error" in severities:
        return "failed"
    if "warning" in severities:
        return "degraded"
    return "healthy"
