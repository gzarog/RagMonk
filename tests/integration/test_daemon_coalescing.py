"""``Daemon``'s per-source pass coalescing (indexing optimization plan,
Phase P1 / findings F1, F3): a burst of triggers for the same source
must never queue one full pass per event -- it collapses into at most
one queued pass plus at most one follow-up pass for whatever arrived
while a pass was already running. See ``service/daemon.py``'s
``_pending_state`` docstring.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ragmonk.core.lifecycle import AppContext
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


def test_burst_of_enqueues_for_one_source_runs_at_most_two_passes(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        run_count = 0
        run_count_lock = threading.Lock()
        original_run_pass = daemon._run_pass

        def counting_run_pass(source_id: str) -> None:
            nonlocal run_count
            with run_count_lock:
                run_count += 1
            time.sleep(0.1)  # keep the pass "running" long enough to overlap triggers
            original_run_pass(source_id)

        daemon._run_pass = counting_run_pass  # type: ignore[method-assign]

        daemon.start()
        try:
            assert _wait_until(lambda: run_count >= 1)

            # A burst of 20 triggers for the same source, fired while the
            # startup pass (or its immediate successor) may still be
            # in-flight. Without coalescing this would queue up to 20
            # more full passes; with it, at most one follow-up pass.
            for _ in range(20):
                daemon.enqueue_source(source.id, reason="test_burst")

            # Give the worker time to drain: the startup pass, plus at
            # most one coalesced follow-up.
            time.sleep(1.0)
            with run_count_lock:
                final_count = run_count
        finally:
            daemon.stop()

    assert final_count <= 2, f"expected at most 2 passes, got {final_count}"


def test_enqueue_after_pass_completes_still_schedules_a_new_pass(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Coalescing must never *drop* a trigger that arrives after the
    previous pass has fully finished (not while it was running) -- only
    triggers that arrive *during* an in-flight pass collapse together.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    with AppContext.bootstrap(
        cli_overrides={"indexing": {"reconciliation_interval_seconds": 3600}}
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))

        daemon = Daemon(ctx)
        run_count = 0
        run_count_lock = threading.Lock()
        original_run_pass = daemon._run_pass

        def counting_run_pass(source_id: str) -> None:
            nonlocal run_count
            with run_count_lock:
                run_count += 1
            original_run_pass(source_id)

        daemon._run_pass = counting_run_pass  # type: ignore[method-assign]

        daemon.start()
        try:
            assert _wait_until(lambda: run_count >= 1)
            time.sleep(0.2)  # let the startup pass fully settle
            with run_count_lock:
                after_startup = run_count

            daemon.enqueue_source(source.id, reason="test_followup")
            assert _wait_until(lambda: run_count > after_startup)
        finally:
            daemon.stop()
