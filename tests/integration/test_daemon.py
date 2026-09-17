"""``service.daemon.Daemon``: watcher-triggered indexing, periodic
reconciliation as a safety net independent of watcher events, and
graceful shutdown mid-pass. Drives ``Daemon`` directly (not through
``ragmonk watch``'s real-signal, real-subprocess entrypoint -- see
``cli/watch.py``'s docstring) so every test here stays fast and
deterministic.
"""

from __future__ import annotations

import time
from pathlib import Path

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext, RunLock
from ragmonk.core.models import FileStatus
from ragmonk.service.daemon import Daemon
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import files_repo, jobs_repo

_WAIT_TIMEOUT_SECONDS = 8.0
_POLL_SECONDS = 0.1


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SECONDS) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SECONDS)
    return predicate()


def _indexed_paths(ctx: AppContext, source_dir: Path, source_id: str) -> set[str]:
    conn = ctx.project_conn(paths.project_id_for_path(source_dir))
    return {f.path for f in files_repo.list_by_source(conn, source_id)}


def test_daemon_indexes_a_file_created_after_start_via_watcher(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"debounce_ms": 50, "reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(
                lambda: any(p.endswith("a.py") for p in _indexed_paths(ctx, source_dir, source.id))
            )

            (source_dir / "b.py").write_text("y = 2\n")
            assert _wait_until(
                lambda: any(p.endswith("b.py") for p in _indexed_paths(ctx, source_dir, source.id))
            )
        finally:
            daemon.stop()


def test_reconciliation_catches_a_change_the_watcher_missed(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    with AppContext.bootstrap(
        cli_overrides={
            # A debounce this long means the local watcher's own trigger
            # structurally cannot fire within this test -- only the
            # reconciliation timer can be responsible for what this test
            # asserts.
            "indexing": {"debounce_ms": 10_000_000, "reconciliation_interval_seconds": 1}
        }
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(
                lambda: any(p.endswith("a.py") for p in _indexed_paths(ctx, source_dir, source.id))
            )

            (source_dir / "b.py").write_text("y = 2\n")
            assert _wait_until(
                lambda: any(p.endswith("b.py") for p in _indexed_paths(ctx, source_dir, source.id)),
                timeout=6.0,
            )
        finally:
            daemon.stop()


def test_graceful_shutdown_finishes_in_flight_pass_and_releases_lock(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    for i in range(20):
        (source_dir / f"f{i}.dat").write_text("x")  # FileKind.UNKNOWN -> raw_processor
    monkeypatch.chdir(tmp_path)

    original_raw_processor = coordinator_module.raw_processor

    def slow_raw_processor(
        ctx: coordinator_module.ProcessorContext,
    ) -> coordinator_module.ProcessingOutcome:
        time.sleep(0.05)
        return original_raw_processor(ctx)

    monkeypatch.setattr(coordinator_module, "raw_processor", slow_raw_processor)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()  # enqueues the initial full pass, ~20 * 0.05s = ~1s
        time.sleep(0.15)  # make sure the pass is genuinely underway

        stop_started = time.monotonic()
        daemon.stop(timeout=10)
        stop_duration = time.monotonic() - stop_started

        # A graceful stop() waits for the in-flight pass rather than
        # abandoning it -- it must take noticeably longer than the
        # worker loop's own idle-poll interval.
        assert stop_duration > 0.3

        conn = ctx.project_conn(paths.project_id_for_path(source_dir))
        files = files_repo.list_by_source(conn, source.id)
        assert len(files) == 20
        assert all(f.status is FileStatus.INDEXED for f in files)
        assert jobs_repo.queue_depth(conn) == 0

    # The per-pass RunLock must have been released cleanly: a fresh
    # acquire from a brand-new lock handle must not block.
    lock = RunLock(paths.locks_dir(ragmonk_home) / "index.lock")
    lock.acquire()
    lock.release()
