"""Live indexing progress snapshot: what ``ragmonk status`` reads to show
which source/stage an indexing run is on, and whether it is still moving,
*without* talking to the indexing process (same no-RPC design as the
daemon's ``service/health.py`` heartbeat).

Every indexing entry point (``ragmonk index``, ``ragmonk rebuild``, the
daemon and the admin UI background indexer) wraps its run in
:func:`track`. The shared runner/coordinator report stage transitions and
per-file outcomes through :func:`current`, which is a no-op tracker when
nothing is being tracked -- so library callers and tests that invoke
``run_source_pass`` directly are unaffected.

Writes are coalesced (at most one per ``_MIN_WRITE_INTERVAL`` for counter
updates; stage/source changes and finalization are always written) so a
large pass never pays per-file disk I/O for progress reporting.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ragmonk.core import paths

SCHEMA_VERSION = 1
_MIN_WRITE_INTERVAL = 1.0


@dataclass
class IndexProgress:
    schema_version: int = SCHEMA_VERSION
    running: bool = False
    pid: int | None = None
    operation: str | None = None
    # "running" | "completed" | "failed"
    outcome: str = "running"
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    source_id: str | None = None
    source_position: int | None = None
    source_total: int | None = None
    stage: str | None = None
    scanned: int = 0
    queued: int = 0
    processing: int = 0
    retry: int = 0
    indexed: int = 0
    failed: int = 0
    error: str | None = None


_FIELD_TYPES: dict[str, type] = {
    "schema_version": int,
    "running": bool,
    "pid": int,
    "operation": str,
    "outcome": str,
    "started_at": str,
    "updated_at": str,
    "completed_at": str,
    "source_id": str,
    "source_position": int,
    "source_total": int,
    "stage": str,
    "scanned": int,
    "queued": int,
    "processing": int,
    "retry": int,
    "indexed": int,
    "failed": int,
    "error": str,
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_progress(home: Path, progress: IndexProgress) -> None:
    path = paths.index_progress_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename with a pid+tid-unique tmp name, exactly like
    # ``service/health.write_health``: a reader in another process must
    # never observe a torn file, and concurrent writers must never share a
    # tmp path.
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(asdict(progress)), encoding="utf-8")
    os.replace(tmp_path, path)


def read_progress(home: Path) -> IndexProgress | None:
    """Tolerant reader: missing, malformed or incompatible snapshots read
    as ``None`` (progress unavailable), never as an error. Unknown keys
    are ignored and wrongly-typed known keys fall back to defaults.
    """
    path = paths.index_progress_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        return None
    kwargs: dict[str, Any] = {}
    for name, expected in _FIELD_TYPES.items():
        value = data.get(name)
        if value is None:
            continue
        if expected is int and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            kwargs[name] = value
    try:
        return IndexProgress(**kwargs)
    except TypeError:
        return None


class ProgressTracker:
    """Owns one run's :class:`IndexProgress` and persists it, coalescing
    counter-only updates. Thread-safe: the coordinator's writer thread is
    the only per-file caller, but finalization may run elsewhere.
    """

    def __init__(self, home: Path | None, operation: str, source_total: int | None) -> None:
        self._home = home
        self._lock = threading.Lock()
        self._last_write = 0.0
        now = now_iso()
        self.progress = IndexProgress(
            running=True,
            pid=os.getpid(),
            operation=operation,
            started_at=now,
            updated_at=now,
            source_total=source_total,
            stage="starting",
        )
        self._flush(force=True)

    def _flush(self, *, force: bool) -> None:
        if self._home is None:
            return
        monotonic = time.monotonic()
        if not force and monotonic - self._last_write < _MIN_WRITE_INTERVAL:
            return
        self.progress.updated_at = now_iso()
        self._last_write = monotonic
        # Progress is diagnostic: a failed write must never break indexing.
        with contextlib.suppress(OSError):
            write_progress(self._home, self.progress)

    def begin_source(self, source_id: str, position: int | None = None) -> None:
        with self._lock:
            self.progress.source_id = source_id
            if position is not None:
                self.progress.source_position = position
            else:
                self.progress.source_position = (self.progress.source_position or 0) + 1
            self.progress.stage = "scan"
            self.progress.queued = 0
            self.progress.processing = 0
            self._flush(force=True)

    def stage(self, name: str) -> None:
        with self._lock:
            if self.progress.stage == name:
                return
            self.progress.stage = name
            self._flush(force=True)

    def scanned(self, count: int, queued: int = 0) -> None:
        with self._lock:
            self.progress.scanned += count
            self.progress.queued = queued
            self._flush(force=False)

    def file_done(self, outcome: str) -> None:
        """``outcome`` is ``indexed``, ``failed``, ``retry`` or ``skipped``."""
        with self._lock:
            if outcome == "indexed":
                self.progress.indexed += 1
            elif outcome == "failed":
                self.progress.failed += 1
            elif outcome == "retry":
                self.progress.retry += 1
            if self.progress.queued > 0:
                self.progress.queued -= 1
            self._flush(force=False)

    def finish(self, *, error: str | None = None) -> None:
        with self._lock:
            now = now_iso()
            self.progress.running = False
            self.progress.outcome = "failed" if error else "completed"
            self.progress.error = error
            self.progress.completed_at = now
            self.progress.stage = "done"
            self.progress.queued = 0
            self.progress.processing = 0
            self._flush(force=True)


class _NullTracker(ProgressTracker):
    def __init__(self) -> None:  # noqa: D107 - deliberately inert
        super().__init__(None, "none", None)


_NULL = _NullTracker()
_ACTIVE: contextvars.ContextVar[ProgressTracker | None] = contextvars.ContextVar(
    "ragmonk_index_progress", default=None
)


def current() -> ProgressTracker:
    """The tracker for the run in progress on this thread/context, or an
    inert one (never ``None``) so instrumented code needs no guards.
    """
    return _ACTIVE.get() or _NULL


@contextlib.contextmanager
def track(
    home: Path, *, operation: str, source_total: int | None = None
) -> Iterator[ProgressTracker]:
    """Tracks one indexing run. Re-entrant: nested calls (``ragmonk
    rebuild`` calling the shared runner) reuse the outer tracker. The
    final snapshot is written on success *and* on exceptions.
    """
    existing = _ACTIVE.get()
    if existing is not None:
        yield existing
        return
    tracker = ProgressTracker(home, operation, source_total)
    token = _ACTIVE.set(tracker)
    try:
        yield tracker
    except BaseException as exc:
        from ragmonk.backends.factory import redact_urls_in_text

        tracker.finish(error=f"{type(exc).__name__}: {redact_urls_in_text(str(exc))}"[:500])
        raise
    else:
        tracker.finish()
    finally:
        _ACTIVE.reset(token)
