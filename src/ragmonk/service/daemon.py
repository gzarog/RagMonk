"""The Phase 7 daemon loop.

Owns source monitoring, the indexing trigger queue, and periodic
reconciliation, funneling every signal -- a debounced local filesystem
event, a network source's poll tick finding a change, a reconciliation
timer firing, or the one-time startup sweep -- into the *same*
``indexing.runner.run_source_pass`` (``IndexCoordinator.run()`` +
Phase 4's linking pass) that ``ragmonk index`` calls directly. This
module is a pure, thread-driven orchestration layer with no process-level
concerns (signal handling, PID files, stdio) of its own -- those belong
to ``cli/watch.py`` (the foreground entrypoint) and ``cli/daemon.py``
(background start/stop), keeping ``Daemon`` itself fully unit-testable.

Locking: a fresh ``core.lifecycle.RunLock`` is acquired and released
around each individual pass (not held for the daemon's whole lifetime).
Passes are additionally serialized through one worker thread and queue,
so in practice at most one pass ever runs at a time in this process; the
lock's job is purely to keep the daemon and a concurrent manual
``ragmonk index`` invocation (a second process) from interleaving their
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

from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext, RunLock
from ragmonk.core.models import Source, SourceStatus, SourceType
from ragmonk.indexing.coordinator import ScanRequest
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.service import health
from ragmonk.sources.registry import SourceRegistry
from ragmonk.telemetry.logging import get_logger, log_event
from ragmonk.watcher.local import LocalSourceWatcher
from ragmonk.watcher.network import NetworkSourceWatcher

_logger = get_logger("daemon")

# The worker loop's queue.get() timeout: how promptly stop() is noticed
# once the queue is empty. Not a user-facing setting -- short enough that
# `daemon stop` never feels slow, long enough not to busy-loop.
_WORKER_POLL_SECONDS = 0.2

# Indexing optimization plan, Phase P2: a batch of touched paths larger
# than this forces a full scan instead of a targeted one -- both a
# watcher-overflow proxy (a burst this large plausibly means something
# structural changed, not N individually-meaningful edits) and a bound
# on per-pass work, matching the plan's "force full scan on watcher
# overflow" guidance without needing to detect a real watchdog-queue
# overflow event specifically.
_MAX_TARGETED_PATHS = 200

# Reasons that always force a full scan+diff pass regardless of what
# (if anything) was accumulated in ``_touched_paths`` -- the periodic
# correctness fallback (P1) and the one-time startup sweep must never
# be narrowed to "whatever happened to be touched since".
_FORCE_FULL_REASONS = frozenset({"startup", "reconciliation"})


class Daemon:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._processors = build_processor_registry(ctx.config)
        self._registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        self._reconciliation_interval = float(ctx.config.indexing.reconciliation_interval_seconds)

        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._local_watchers: dict[str, LocalSourceWatcher] = {}
        self._network_watchers: dict[str, NetworkSourceWatcher] = {}
        self._worker_thread: threading.Thread | None = None
        self._reconciliation_thread: threading.Thread | None = None

        # Indexing optimization plan, Phase P1: per-source scheduling
        # state, guarded by ``_state_lock`` below. "queued" means a pass
        # is sitting in ``self._queue`` but hasn't started; "running"
        # means ``_run_pass`` is currently executing for it; "running_
        # followup" means a *further* trigger arrived while it was
        # running, so exactly one more pass is queued the moment the
        # current one finishes. A source id absent from this dict has no
        # pending or in-flight work at all. This collapses an arbitrary
        # burst of watcher events for the same source into at most one
        # queued pass plus at most one follow-up pass -- never the
        # unbounded one-``queue.put`` per event the plain ``Queue`` above
        # allowed before this phase (finding F1/F3).
        self._pending_state: dict[str, str] = {}
        # Indexing optimization plan, Phase P2: paths accumulated for
        # this source's *next* pass (guarded by ``_state_lock``,
        # cleared/consumed in ``_build_scan_request`` right before that
        # pass starts) and whether that next pass must be full
        # regardless -- set whenever any trigger contributing to it was
        # a startup/reconciliation sweep (``_FORCE_FULL_REASONS``).
        # Coalescing (``_pending_state`` above) decides *whether* a pass
        # runs; this decides *what kind* once it does.
        self._touched_paths: dict[str, set[str]] = {}
        self._force_full: dict[str, bool] = {}
        # Indexing optimization plan V2, Phase P5: the *real* trigger
        # reason for this source's next pass -- startup/reconciliation/
        # network_watcher/local_watcher/manual -- accumulated and
        # consumed the same way ``_touched_paths``/``_force_full`` above
        # are: set once (first reason wins) when a new pending batch
        # starts, popped in ``_build_scan_request`` right before that
        # pass runs. Previously ``_build_scan_request`` hardcoded
        # ``reason="daemon"`` on every ``ScanRequest`` regardless of what
        # actually triggered it -- this is what makes
        # ``daemon_pass_completed``'s telemetry (and any other consumer
        # of ``ScanRequest.reason``) traceable back to a real cause
        # instead of one constant string.
        self._trigger_reasons: dict[str, str] = {}

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
            target=self._worker_loop, name="ragmonk-daemon-worker", daemon=True
        )
        self._worker_thread.start()
        self._reconciliation_thread = threading.Thread(
            target=self._reconciliation_loop, name="ragmonk-daemon-reconcile", daemon=True
        )
        self._reconciliation_thread.start()

        # One immediate full pass per source on startup -- otherwise a
        # source changed while the daemon was stopped would sit untouched
        # until the first watcher event or the first reconciliation tick,
        # up to `reconciliation_interval_seconds` away.
        for source_id in list(self._online):
            self.enqueue_source(source_id, reason="startup")

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

    def enqueue_source(self, source_id: str, *, reason: str = "trigger") -> None:
        """Coalesces a burst of triggers for the same source into at
        most one queued pass plus at most one follow-up pass -- see
        ``_pending_state``'s docstring above. ``reason`` is telemetry
        only (a watcher event, a reconciliation tick, daemon startup,
        ...); it never affects scheduling.
        """
        if self._stop_event.is_set():
            return
        with self._state_lock:
            if reason in _FORCE_FULL_REASONS:
                self._force_full[source_id] = True
            # First reason wins for whichever pending batch this trigger
            # contributes to -- mirrors _touched_paths/_force_full above:
            # accumulated unconditionally on every trigger, popped as one
            # unit in _build_scan_request. A burst of mixed-reason
            # triggers (e.g. a local_watcher event followed by a
            # reconciliation tick before the pass starts) reports the
            # *first* one, on the reasoning that it's what actually
            # caused this batch to start accumulating in the first place.
            self._trigger_reasons.setdefault(source_id, reason)
            state = self._pending_state.get(source_id)
            if state is None:
                self._pending_state[source_id] = "queued"
            elif state == "queued":
                # Already sitting in the queue, not yet started -- this
                # trigger is absorbed into that pending pass.
                return
            elif state == "running":
                self._pending_state[source_id] = "running_followup"
                return
            else:  # "running_followup" -- a follow-up is already scheduled
                return
        log_event(_logger, "source_pass_queued", source_id=source_id, reason=reason)
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
            self.enqueue_source(source.id, reason="reconciliation")
        with self._state_lock:
            self._last_reconciliation_at = health.now_iso()
        self._write_health()

    # -- internals ----------------------------------------------------------

    def _network_trigger(self, source_id: str) -> Callable[[set[str]], None]:
        def _trigger(changed_paths: set[str]) -> None:
            # Indexing optimization plan, Phase P2 / finding F2: the
            # poll tick that detected this change already computed
            # exactly which paths changed (see ``NetworkSourceWatcher.
            # poll_once``) -- reused for a targeted pass instead of the
            # coordinator walking the network tree a second time.
            with self._state_lock:
                self._touched_paths.setdefault(source_id, set()).update(changed_paths)
            self.enqueue_source(source_id, reason="network_watcher")

        return _trigger

    def _local_trigger(self, source_id: str) -> Callable[[Path], None]:
        def _trigger(path: Path) -> None:
            with self._state_lock:
                self._touched_paths.setdefault(source_id, set()).add(str(path))
            self.enqueue_source(source_id, reason="local_watcher")

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
            with self._state_lock:
                self._pending_state[source_id] = "running"
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
                self._settle_pending_state(source_id)
                self._queue.task_done()

    def _settle_pending_state(self, source_id: str) -> None:
        """Runs once a pass for ``source_id`` has finished (successfully
        or not): if a follow-up trigger arrived while it was running,
        queue exactly one more pass now; otherwise the source has no
        pending work left. See ``_pending_state``'s docstring.
        """
        requeue = False
        with self._state_lock:
            state = self._pending_state.pop(source_id, None)
            if state == "running_followup":
                self._pending_state[source_id] = "queued"
                requeue = True
        if requeue:
            self._queue.put(source_id)

    def _build_scan_request(self, source_id: str) -> ScanRequest:
        """Consumes (pops) this source's accumulated touched-paths/
        force-full state for the pass about to run -- whatever arrives
        after this point starts a fresh accumulation for the *next*
        pass, exactly like ``_settle_pending_state`` scopes coalescing
        to one pass at a time.
        """
        with self._state_lock:
            touched = self._touched_paths.pop(source_id, None)
            force_full = self._force_full.pop(source_id, False)
            # Indexing optimization plan V2, Phase P5: the real trigger
            # reason, not a constant "daemon" string -- falls back to
            # "daemon" only for the never-actually-expected case of a
            # pass with no recorded reason at all (e.g. a queue entry
            # from before this phase's own bookkeeping existed, which
            # cannot happen in practice since every enqueue_source call
            # sets one, but a bare fallback is cheaper than an assert
            # here).
            reason = self._trigger_reasons.pop(source_id, "daemon")
        if force_full or not touched or len(touched) > _MAX_TARGETED_PATHS:
            return ScanRequest(source_id=source_id, reason=reason, full=True)
        return ScanRequest(
            source_id=source_id, reason=reason, changed_paths=frozenset(touched), full=False
        )

    def _run_pass(self, source_id: str) -> None:
        scan_request = self._build_scan_request(source_id)
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
                    pass_result = run_source_pass(
                        self._ctx, source, self._processors, scan_request=scan_request
                    )
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
            scan_incomplete=pass_result.result.scan_incomplete,
            targeted=pass_result.result.targeted,
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
