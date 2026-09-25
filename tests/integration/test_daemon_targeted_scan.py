"""``Daemon``'s Phase P2 wiring: a local watcher event for one file
drives a *targeted* pass over just that path, not a full source scan
(findings F1/F2), while a burst large enough to look like a watcher
overflow -- or a reconciliation tick -- still forces a full scan.
"""

from __future__ import annotations

import time
from pathlib import Path

import ragmonk.service.daemon as daemon_module
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing.coordinator import ScanRequest
from ragmonk.service.daemon import Daemon
from ragmonk.sources.registry import SourceRegistry

_WAIT_TIMEOUT_SECONDS = 8.0
_POLL_SECONDS = 0.05


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SECONDS) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SECONDS)
    return predicate()


def test_a_single_local_edit_drives_a_targeted_not_full_pass(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    (source_dir / "b.py").write_text("y = 2\n")
    monkeypatch.chdir(tmp_path)

    captured: list[ScanRequest] = []
    original_run_source_pass = daemon_module.run_source_pass

    def spying_run_source_pass(ctx, source, processors, *, scan_request=None):  # noqa: ANN001, ANN202
        captured.append(scan_request)
        return original_run_source_pass(ctx, source, processors, scan_request=scan_request)

    monkeypatch.setattr(daemon_module, "run_source_pass", spying_run_source_pass)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"debounce_ms": 50, "reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            # Startup pass: always full.
            assert _wait_until(lambda: len(captured) >= 1)
            assert captured[0].full is True

            edited = source_dir / "a.py"
            edited.write_text("x = 2\n")
            assert _wait_until(lambda: len(captured) >= 2)

            targeted_request = captured[1]
            assert targeted_request.full is False
            assert targeted_request.changed_paths == frozenset({str(edited.resolve())})
        finally:
            daemon.stop()


def test_a_large_burst_forces_a_full_scan_instead_of_targeted(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "seed.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    captured: list[ScanRequest] = []
    original_run_source_pass = daemon_module.run_source_pass

    def spying_run_source_pass(ctx, source, processors, *, scan_request=None):  # noqa: ANN001, ANN202
        captured.append(scan_request)
        return original_run_source_pass(ctx, source, processors, scan_request=scan_request)

    monkeypatch.setattr(daemon_module, "run_source_pass", spying_run_source_pass)
    monkeypatch.setattr(daemon_module, "_MAX_TARGETED_PATHS", 3)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"debounce_ms": 50, "reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(lambda: len(captured) >= 1)

            # A burst of edits well past the (patched, small) threshold.
            with daemon._state_lock:
                daemon._touched_paths[source.id] = {
                    str(source_dir / f"f{i}.py") for i in range(10)
                }
            daemon.enqueue_source(source.id, reason="local_watcher")
            assert _wait_until(lambda: len(captured) >= 2)

            assert captured[1].full is True
        finally:
            daemon.stop()


def test_reconciliation_forces_full_even_with_touched_paths_pending(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    captured: list[ScanRequest] = []
    original_run_source_pass = daemon_module.run_source_pass

    def spying_run_source_pass(ctx, source, processors, *, scan_request=None):  # noqa: ANN001, ANN202
        captured.append(scan_request)
        return original_run_source_pass(ctx, source, processors, scan_request=scan_request)

    monkeypatch.setattr(daemon_module, "run_source_pass", spying_run_source_pass)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"debounce_ms": 50, "reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(lambda: len(captured) >= 1)

            with daemon._state_lock:
                daemon._touched_paths[source.id] = {str(source_dir / "a.py")}
            daemon.reconcile_now()
            assert _wait_until(lambda: len(captured) >= 2)

            assert captured[1].full is True
        finally:
            daemon.stop()
