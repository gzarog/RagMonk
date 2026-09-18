"""Indexing controls, failed-file inspection and live progress.

Admin UI plan, Phase 3 (§6): the UI starts indexing, rebuilds sources
and watches progress. The heavy lifting is done by the existing
``run_source_pass`` / ``ops.rebuild`` code -- this module adds only the
pieces the UI needs that the CLI gets for free from its synchronous,
foreground execution model:

* a **background runner** (:class:`BackgroundIndexer`) so an HTTP request
  returns immediately while the pass runs on a worker thread with its own
  ``AppContext`` (the request's connections are not safe to hand to
  another thread, and the run must hold the same ``index`` lock the CLI
  takes -- see ``ragmonk index``);
* a **progress event stream** the SSE endpoint (§6.5) tails.

Progress events are per-source (``scan_started`` / ``scan_completed``
carrying that source's result counts) rather than per-file: the
underlying ``IndexCoordinator`` runs a whole pass and returns a summary,
it does not expose per-file callbacks, so per-source is the finest-grained
honest progress available today without re-plumbing the coordinator.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.ops.rebuild import rebuild as run_rebuild
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import files_repo, jobs_repo


@dataclass
class ProgressEvent:
    seq: int
    kind: str
    payload: dict[str, Any]
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "at": self.at, **self.payload}


class BackgroundIndexer:
    """A process-wide singleton that runs at most one indexing pass at a
    time on a background thread and records progress events.

    Concurrency protection (Admin UI plan §11.3): the ``index`` runtime
    lock, taken inside the worker with a fresh ``AppContext``, is the same
    lock ``ragmonk index`` / ``ragmonk rebuild`` take, so a UI-triggered
    run and a CLI run can never index the same runtime concurrently. The
    ``_active`` flag additionally rejects a second UI trigger while one is
    already running.
    """

    _MAX_EVENTS = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active = False
        self._events: deque[ProgressEvent] = deque(maxlen=self._MAX_EVENTS)
        self._seq = 0
        self._last_summary: dict[str, Any] | None = None

    # -- state --------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._active,
                "last_summary": self._last_summary,
                "events": [e.as_dict() for e in self._events],
            }

    def events_since(self, after_seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e.as_dict() for e in self._events if e.seq > after_seq]

    def _emit(self, kind: str, **payload: Any) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(ProgressEvent(self._seq, kind, payload))

    # -- triggers -----------------------------------------------------------

    def start(self, *, source_id: str | None = None) -> bool:
        """Kick off an indexing pass. Returns ``False`` if one is already
        running (the caller surfaces "already running" to the user)."""
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"source_id": source_id, "rebuild": False, "fresh": False},
                name="ragmonk-ui-indexer",
                daemon=True,
            )
        self._thread.start()
        return True

    def rebuild(self, *, source_id: str | None = None, fresh: bool = False) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"source_id": source_id, "rebuild": True, "fresh": fresh},
                name="ragmonk-ui-rebuilder",
                daemon=True,
            )
        self._thread.start()
        return True

    # -- worker -------------------------------------------------------------

    def _run(self, *, source_id: str | None, rebuild: bool, fresh: bool) -> None:
        mode = "rebuild" if rebuild else "index"
        self._emit("run_started", mode=mode, source_id=source_id, fresh=fresh)
        summary: dict[str, Any] = {"mode": mode, "sources": [], "failed": 0}
        try:
            with AppContext.bootstrap() as ctx:
                lock = ctx.acquire_lock("index")
                try:
                    if rebuild:
                        summary = self._do_rebuild(ctx, source_id=source_id, fresh=fresh)
                    else:
                        summary = self._do_index(ctx, source_id=source_id)
                finally:
                    lock.release()
            self._emit("run_completed", **summary)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, never a traceback
            summary["error"] = str(exc)
            self._emit("run_failed", error=str(exc))
        finally:
            with self._lock:
                self._active = False
                self._last_summary = summary

    def _do_index(self, ctx: AppContext, *, source_id: str | None) -> dict[str, Any]:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        sources = [registry.get(source_id)] if source_id else registry.list(enabled_only=True)
        processors = build_processor_registry(ctx.config)
        per_source: list[dict[str, Any]] = []
        total_failed = 0
        for source in sources:
            self._emit("scan_started", source_id=source.id, path=source.path)
            pass_result = run_source_pass(ctx, source, processors)
            result = pass_result.result
            row = {
                "source_id": source.id,
                "path": source.path,
                "offline": result.source_offline,
                "scanned": result.scanned,
                "indexed": result.indexed,
                "failed": result.failed,
            }
            total_failed += result.failed
            per_source.append(row)
            self._emit("scan_completed", **row)
        return {"mode": "index", "sources": per_source, "failed": total_failed}

    def _do_rebuild(
        self, ctx: AppContext, *, source_id: str | None, fresh: bool
    ) -> dict[str, Any]:
        self._emit("rebuild_started", source_id=source_id, fresh=fresh)
        outcomes = run_rebuild(ctx, source_id=source_id, fresh=fresh)
        per_source = []
        total_failed = 0
        for outcome in outcomes:
            row = {
                "source_id": outcome.source.id,
                "path": outcome.source.path,
                "scanned": outcome.result.scanned,
                "indexed": outcome.result.indexed,
                "failed": outcome.result.failed,
            }
            total_failed += outcome.result.failed
            per_source.append(row)
            self._emit("scan_completed", **row)
        return {"mode": "rebuild", "sources": per_source, "failed": total_failed}


# One instance for the whole process. The UI app uses this; the CLI does
# not (it runs indexing synchronously in the foreground) -- but both go
# through the same ``index`` runtime lock, so they stay mutually exclusive.
INDEXER = BackgroundIndexer()


def indexing_overview(ctx: AppContext) -> dict[str, Any]:
    """Queue depth, per-status file counts and failed files for §6.1."""
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    total_queue = 0
    totals: dict[str, int] = {}
    for source in registry.list():
        project_id = _project_id(source.path)
        conn = ctx.project_conn(project_id)
        total_queue += jobs_repo.queue_depth(conn)
        for key, value in files_repo.count_by_status(conn, source.id).items():
            totals[key] = totals.get(key, 0) + value
    snap = INDEXER.snapshot()
    return {
        "queue_depth": total_queue,
        "counts": totals,
        "running": snap["running"],
        "last_summary": snap["last_summary"],
    }


def failed_files(ctx: AppContext) -> list[dict[str, Any]]:
    """Every file currently in FAILED status, across all sources (§6.4)."""
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    failed: list[dict[str, Any]] = []
    for source in registry.list():
        project_id = _project_id(source.path)
        conn = ctx.project_conn(project_id)
        for record in files_repo.list_by_source(conn, source.id):
            if record.status.value == "failed":
                failed.append(
                    {
                        "file_id": record.id,
                        "source_id": source.id,
                        "path": record.path,
                        "error": record.last_error,
                        "updated_at": record.updated_at,
                    }
                )
    return failed


def _project_id(source_path: str) -> str:
    from ragmonk.core import paths

    return paths.project_id_for_path(Path(source_path))
