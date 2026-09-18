"""The Phase 7 daemon loop.

Owns source monitoring, the indexing trigger queue, and periodic
reconciliation, funneling every signal -- a debounced local filesystem
event, a network source's poll tick finding a change, a reconciliation
timer firing, or the one-time startup sweep -- into the *same*
``indexing.runner.run_source_pass`` (``IndexCoordinator.run()`` +
Phase 4's linking pass) that ``ragpilot index`` calls directly. This
module is a pure, thread-driven orchestration layer with no process-level
concerns (signal handling, PID files, stdio) of its own -- those belong
to ``cli/watch.py`` (the foreground entrypoint) and ``cli/daemon.py``
(background start/stop), keeping ``Daemon`` itself fully unit-testable.

Locking: a fresh ``core.lifecycle.RunLock`` is acquired and released
around each individual pass (not held for the daemon's whole lifetime).
Passes are additionally serialized through one worker thread and queue,
so in practice at most one pass ever runs at a time in this process; the
lock's job is purely to keep the daemon and a concurrent manual
``ragpilot index`` invocation (a second process) from interleaving their
writes to the same database -- the exact protection Phase 1 built it
for, reused unchanged rather than replaced with a daemon-specific
mechanism.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from ragpilot.core import paths
from ragpilot.core.errors import UsageError
from ragpilot.core.lifecycle import AppContext, RunLock
from ragpilot.core.models import Source, SourceStatus, SourceType
from ragpilot.indexing.runner import build_processor_registry, run_source_pass
from ragpilot.service import health
from ragpilot.sources.registry import SourceRegistry
from ragpilot.telemetry.logging import get_logger, log_event
from ragpilot.watcher.local import LocalSourceWatcher
from ragpilot.watcher.network import NetworkSourceWatcher

_logger = get_logger("daemon")

# The worker loop's queue.get() timeout: how promptly stop() is noticed
# once the queue is empty. Not a user-facing setting -- short enough that
# `daemon stop` never feels slow, long enough not to busy-loop.
_WORKER_POLL_SECONDS = 0.2


class Daemon:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._processors = build_processor_registry(ctx.config)
        self._registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        self._reconciliation_interval = float(
            ctx.config.indexing.reconciliation_interval_seconds
        )

        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._local_watchers: dict[str, LocalSourceWatcher] = {}
        self._network_watchers: dict[str, NetworkSourceWatcher] = {}
        self._worker_thread: threading.Thread | None = None
        self._reconciliation_thread: threading.Thread | None = None

        # ``ctx.sources_conn`` (and, transitively, each project
        # connection ``run_source_pass`` opens) is shared across the
        # worker thread, the reconciliation thread and whichever thread
        # calls start()/stop() -- sqlite3 connections aren't safe for
        # concurrent use from multiple threads even with
        # ``check_same_thread=False`` (storage/sqlite.py), so every touch
        # of ``self._registry`` or ``run_source_pass`` is serialized
        # through this lock. Separate from ``_state_lock`` below, which
        # only guards this object's own in-memory bookkeeping.
        self._db_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started_at = health.now_iso()
        self._last_reconciliation_at: str | None = None
        self._last_pass_at: dict[str, str] = {}
        self._online: dict[str, bool] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        with self._db_lock:
            sources = self._registry.list(enabled_only=True)
        for source in sources:
            self._online[source.id] = source.status is not SourceStatus.OFFLINE
            self._attach_watcher(source)

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="ragpilot-daemon-worker", daemon=True
        )
        self._worker_thread.start()
        self._reconciliation_thread = threading.Thread(
            target=self._reconciliation_loop, name="ragpilot-daemon-reconcile", daemon=True
        )
        self._reconciliation_thread.start()

        # One immediate full pass per source on startup -- otherwise a
        # source changed while the daemon was stopped would sit untouched
        # until the first watcher event or the first reconciliation tick,
        # up to `reconciliation_interval_seconds` away.
        for source_id in list(self._online):
            self.enqueue_source(source_id)

        self._write_health()
        log_event(_logger, "daemon_started", sources=len(self._online))

    def stop(self, timeout: float | None = 30.0) -> None:
        """Graceful shutdown: stop accepting new triggers, let whatever
        pass is currently in-flight finish naturally (its own per-file
        transactions already commit as they go -- see
        ``storage/sqlite.py``'s ``transaction()`` -- so there is nothing
        to abort mid-write), then release every watcher and thread.

        Setting ``_stop_event`` first is what makes this graceful rather
        than abrupt: ``enqueue_source`` and both loops below check it and
        stop scheduling *new* work immediately, while the worker thread
        is joined (not killed) so a pass already running is never
        interrupted partway through.
        """
        self._stop_event.set()

        for local_watcher in self._local_watchers.values():
            local_watcher.stop()
        for network_watcher in self._network_watchers.values():
            network_watcher.stop()
        self._local_watchers.clear()
        self._network_watchers.clear()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                log_event(
                    _logger,
                    "daemon_worker_join_timeout",
                    level=logging.WARNING,
                    timeout_seconds=timeout,
                )
            self._worker_thread = None
        if self._reconciliation_thread is not None:
            self._reconciliation_thread.join(timeout=timeout)
            self._reconciliation_thread = None

        self._write_health()
        log_event(_logger, "daemon_stopped")

    # -- triggers -----------------------------------------------------------

    def enqueue_source(self, source_id: str) -> None:
        if self._stop_event.is_set():
            return
        self._queue.put(source_id)

    def reconcile_now(self) -> None:
        """Enqueues every enabled source for a full scan+diff pass,
        independent of whatever watcher events have or haven't fired --
        the periodic safety net for a missed/coalesced OS event or a
        dropped poll tick. Also re-attaches a local watcher for any
        source that didn't have one (e.g. its root was inaccessible at
        daemon startup and has since come back).
        """
        if self._stop_event.is_set():
            return
        with self._db_lock:
            sources = self._registry.list(enabled_only=True)
        for source in sources:
            if source.source_type is SourceType.LOCAL and source.id not in self._local_watchers:
                self._attach_watcher(source)
            self.enqueue_source(source.id)
        with self._state_lock:
            self._last_reconciliation_at = health.now_iso()
        self._write_health()

    # -- internals ----------------------------------------------------------

    def _network_trigger(self, source_id: str) -> Callable[[], None]:
        def _trigger() -> None:
            self.enqueue_source(source_id)

        return _trigger

    def _local_trigger(self, source_id: str) -> Callable[[Path], None]:
        def _trigger(_path: Path) -> None:
            self.enqueue_source(source_id)

        return _trigger

    def _attach_watcher(self, source: Source) -> None:
        root = Path(source.path)
        if source.source_type is SourceType.NETWORK:
            network_watcher = NetworkSourceWatcher(
                root,
                include_patterns=source.include_patterns,
                exclude_patterns=source.exclude_patterns,
                interval_seconds=float(self._ctx.config.indexing.network_poll_seconds),
                on_trigger=self._network_trigger(source.id),
                follow_symlinks=self._ctx.config.indexing.follow_symlinks,
            )
            network_watcher.start()
            self._network_watchers[source.id] = network_watcher
            return

        try:
            local_watcher = LocalSourceWatcher(
                root,
                debounce_ms=self._ctx.config.indexing.debounce_ms,
                on_trigger=self._local_trigger(source.id),
            )
            local_watcher.start()
        except OSError:
            # Root not present/listable right now (e.g. offline at daemon
            # startup) -- no live watcher for this source until
            # reconcile_now() successfully retries attaching one; the
            # reconciliation timer still enqueues it on schedule either
            # way, and IndexCoordinator.run() itself handles the
            # offline/empty distinction correctly regardless of whether a
            # watcher exists.
            log_event(
                _logger,
                "local_watcher_attach_failed",
                level=logging.WARNING,
                source_id=source.id,
            )
            return
        self._local_watchers[source.id] = local_watcher

    def _worker_loop(self) -> None:
        while True:
            try:
                source_id = self._queue.get(timeout=_WORKER_POLL_SECONDS)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                self._run_pass(source_id)
            except Exception:
                log_event(
                    _logger,
                    "daemon_pass_error",
                    level=logging.ERROR,
                    source_id=source_id,
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    def _run_pass(self, source_id: str) -> None:
        pass_result = None
        with self._db_lock:
            try:
                source = self._registry.get(source_id)
            except UsageError:
                source = None  # removed since being enqueued
            if source is not None and source.enabled:
                lock = RunLock(paths.locks_dir(self._ctx.home) / "index.lock")
                lock.acquire()
                try:
                    pass_result = run_source_pass(self._ctx, source, self._processors)
                finally:
                    lock.release()

        if pass_result is None:
            return

        with self._state_lock:
            self._last_pass_at[source_id] = health.now_iso()
            self._online[source_id] = not pass_result.result.source_offline
        log_event(
            _logger,
            "daemon_pass_completed",
            source_id=source_id,
            offline=pass_result.result.source_offline,
            indexed=pass_result.result.indexed,
            deleted=pass_result.result.deleted,
            failed=pass_result.result.failed,
        )
        self._write_health()

    def _reconciliation_loop(self) -> None:
        while not self._stop_event.wait(self._reconciliation_interval):
            self.reconcile_now()

    def _write_health(self) -> None:
        with self._db_lock:
            all_sources = self._registry.list()
        with self._state_lock:
            sources = [
                health.SourceWatchStatus(
                    source_id=source.id,
                    path=source.path,
                    source_type=source.source_type.value,
                    online=self._online.get(source.id, source.status is not SourceStatus.OFFLINE),
                    last_pass_at=self._last_pass_at.get(source.id),
                )
                for source in all_sources
            ]
            snapshot = health.DaemonHealth(
                started_at=self._started_at,
                updated_at=health.now_iso(),
                last_reconciliation_at=self._last_reconciliation_at,
                sources=sources,
            )
        health.write_health(self._ctx.home, snapshot)
