"""Index lock scope: CLI per-source release, daemon contention/recovery."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import ragmonk.cli.index as index_cli
import ragmonk.service.daemon as daemon_module
from ragmonk.core import paths
from ragmonk.core.errors import RunLockTimeoutError
from ragmonk.core.lifecycle import AppContext, RunLock, inspect_lock
from ragmonk.service.daemon import Daemon
from ragmonk.sources.registry import SourceRegistry


def _wait_until(predicate, timeout: float = 15.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _make_source(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    return d


def test_cli_releases_lock_between_sources_and_on_error(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    lock_path = paths.locks_dir(ragmonk_home) / "index.lock"
    with AppContext.bootstrap() as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.add(str(_make_source(tmp_path, "one")))
        registry.add(str(_make_source(tmp_path, "two")))

    states: list[str] = []
    original = index_cli.run_source_pass

    def spy(ctx, source, processors, **kw):  # noqa: ANN001, ANN202
        # held during the pass (from another handle's perspective)
        states.append(inspect_lock(lock_path).state)
        if len(states) == 1:
            raise RuntimeError("boom")  # exception must still release
        return original(ctx, source, processors, **kw)

    monkeypatch.setattr(index_cli, "run_source_pass", spy)
    with AppContext.bootstrap() as ctx:
        # invoke the command body directly via typer runner
        from typer.testing import CliRunner

        from ragmonk.cli.main import app

        result = CliRunner().invoke(app, ["index"])
    assert states == ["held", "held"]
    assert result.exit_code != 0  # one source failed
    assert inspect_lock(lock_path).state == "free"
    del ctx


def test_cli_reports_blocked_source_instead_of_hanging(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with AppContext.bootstrap() as ctx:
        SourceRegistry(ctx.sources_conn, home=ctx.home).add(str(_make_source(tmp_path, "one")))
    paths.user_config_path(ragmonk_home).write_text("indexing:\n  lock_timeout_seconds: 0.3\n")
    holder = RunLock(paths.locks_dir(ragmonk_home) / "index.lock", operation="index")
    holder.acquire()
    try:
        from typer.testing import CliRunner

        from ragmonk.cli.main import app

        started = time.monotonic()
        result = CliRunner().invoke(app, ["index"])
        assert time.monotonic() - started < 20
    finally:
        holder.release()
    assert result.exit_code != 0
    assert "blocked" in result.output


def test_daemon_pass_recovers_after_lock_contention(
    ragmonk_home: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon_module, "_CONTENTION_BACKOFF_SECONDS", 0.2)
    source_dir = _make_source(tmp_path, "src")
    with AppContext.bootstrap(
        cli_overrides={
            "indexing": {"lock_timeout_seconds": 0.3, "reconciliation_interval_seconds": 3600}
        }
    ) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))
        holder = RunLock(paths.locks_dir(ragmonk_home) / "index.lock", operation="index")
        holder.acquire()
        daemon = Daemon(ctx)
        try:
            daemon.start()
            # _db_lock must stay free while the worker waits on index.lock
            assert daemon._db_lock.acquire(timeout=1)
            daemon._db_lock.release()
            time.sleep(1.0)  # at least one contended attempt, daemon alive
            assert daemon._worker_thread is not None and daemon._worker_thread.is_alive()
            assert daemon._last_pass_at.get(source.id) is None
            holder.release()
            assert _wait_until(lambda: source.id in daemon._last_pass_at)
            assert _wait_until(lambda: daemon._pending_state.get(source.id) is None)
        finally:
            holder.release()
            daemon.stop(timeout=10)


def test_timeout_error_type_is_raised(ragmonk_home: Path) -> None:
    holder = RunLock(paths.locks_dir(ragmonk_home) / "index.lock")
    holder.acquire()
    try:
        with pytest.raises(RunLockTimeoutError):
            RunLock(paths.locks_dir(ragmonk_home) / "index.lock").acquire(timeout_seconds=0.1)
    finally:
        holder.release()
