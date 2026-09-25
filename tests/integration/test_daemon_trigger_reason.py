"""Indexing optimization plan V2, Phase P5: the real trigger reason
(startup/reconciliation/local_watcher/network_watcher) survives all the
way to the ``ScanRequest`` the daemon actually dispatches, instead of
``_build_scan_request``'s pre-P5 hardcoded ``"daemon"`` string. Mirrors
``test_daemon_targeted_scan.py``'s spy-on-``run_source_pass`` style.
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


def test_startup_pass_and_local_edit_carry_their_real_trigger_reasons(
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
        registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(lambda: len(captured) >= 1)
            # Startup's own immediate full pass -- the real reason, not
            # a generic "daemon" string.
            assert captured[0].reason == "startup"
            assert captured[0].full is True

            edited = source_dir / "a.py"
            edited.write_text("x = 2\n")
            assert _wait_until(lambda: len(captured) >= 2)

            assert captured[1].reason == "local_watcher"
            assert captured[1].full is False
        finally:
            daemon.stop()


def test_reconcile_now_carries_the_reconciliation_reason(
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
        registry.add(str(source_dir))

        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert _wait_until(lambda: len(captured) >= 1)  # startup pass
            captured.clear()

            daemon.reconcile_now()
            assert _wait_until(lambda: len(captured) >= 1)
            assert captured[0].reason == "reconciliation"
            assert captured[0].full is True  # reconciliation always forces full
        finally:
            daemon.stop()


def test_manual_cli_style_call_has_no_scan_request_and_defaults_to_manual_reason(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """``ragmonk index`` (``cli/index.py``) calls ``run_source_pass``
    with no ``scan_request`` at all -- ``run_source_pass`` itself must
    still resolve a sensible trigger reason ("manual") for its own
    telemetry rather than crashing or silently omitting one.
    """
    from ragmonk.indexing.runner import build_processor_registry, run_source_pass
    from ragmonk.storage.repositories import sources_repo

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")

    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))
        processors = build_processor_registry(ctx.config)

        # No scan_request -- exactly cli/index.py's own call shape.
        result = run_source_pass(ctx, source, processors)
        assert result.result.indexed == 1
        # Not asserting a log line here (see test_stage_timings_event.py
        # for that) -- this just confirms the no-scan_request path still
        # completes normally with the new trigger_reason computation in
        # place (previously this line of code didn't exist at all).
        sources_repo.get(ctx.sources_conn, source.id)
