"""Status observability V1, end to end through ``ragmonk status``."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ragmonk.cli import status as status_cli
from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext, RunLock
from ragmonk.service import progress
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import files_repo, jobs_repo


def _setup(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def foo():\n    return 1\n")
    (source_dir / "readme.md").write_text("# Title\n\nSome text.\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    return source_dir


def _status_json(runner: CliRunner) -> dict[str, Any]:
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.output)["data"]
    return data


def _flat(output: str) -> str:
    """Alphanumerics only: Rich wraps long lines (e.g. Windows paths)
    inside panels, so assert on text independent of line breaks/borders.
    """
    return "".join(ch for ch in output if ch.isalnum())


def _source_id() -> str:
    with AppContext.bootstrap() as ctx:
        return SourceRegistry(ctx.sources_conn, home=ctx.home).list()[0].id


def test_status_json_keeps_legacy_fields_and_adds_observability(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(runner, tmp_path, monkeypatch)
    assert runner.invoke(app, ["index"]).exit_code == 0
    data = _status_json(runner)

    # Legacy consumers.
    for key in ("sources", "backend", "totals", "tokenizer"):
        assert key in data
    row = data["sources"][0]
    for key in ("id", "status", "counts", "queue_depth", "last_scan_at", "last_error"):
        assert key in row

    # Additive fields.
    assert data["health"] == {"status": "healthy", "problem_count": 0}
    assert data["indexer"]["state"] == "idle"
    assert data["indexer"]["last_run"]["outcome"] == "completed"
    assert data["queue"]["depth"] == 0
    assert data["recent_errors"] == []
    assert data["problems"] == []
    assert data["sources_with_errors"] == 0
    assert row["access_state"] == "online"
    assert row["index_state"] == "completed"
    assert row["queue"]["queued"] == 0
    assert row["last_activity_at"] is not None

    monkeypatch.setattr(status_cli.console, "_width", 200)
    human = runner.invoke(app, ["status"])
    assert human.exit_code == 0, human.output
    for word in ("Indexer", "Health", "Access", "Index State", "completed"):
        assert word in human.output


def test_status_surfaces_failed_file_and_last_error(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ragmonk.code.processor as processor_module

    def broken_parse(source: bytes, language: str) -> Any:
        raise RuntimeError("simulated parser crash")

    monkeypatch.setattr(processor_module, "parse", broken_parse)
    _setup(runner, tmp_path, monkeypatch)
    runner.invoke(app, ["index"])

    # First attempt: a transient (retry) failure is already visible.
    data = _status_json(runner)
    assert data["health"]["status"] == "degraded"
    assert data["sources"][0]["index_state"] == "retrying"
    latest = data["queue"]["latest_job_error"]
    assert latest["error_code"] == "RuntimeError"
    assert "simulated parser crash" in latest["error_message"]
    assert any("awaiting retry" in p["message"] for p in data["problems"])

    # Retries exhausted: permanent failure, recorded as an index error.
    monkeypatch.setattr("ragmonk.indexing.retry.is_permanent", lambda _attempt: True)
    with AppContext.bootstrap() as ctx:
        conn = ctx.project_conn(paths.project_id_for_path(tmp_path / "src"), control_plane=True)
        conn.execute("UPDATE index_jobs SET next_attempt_at = NULL")
        conn.commit()
    runner.invoke(app, ["index"])

    data = _status_json(runner)
    assert data["health"]["status"] == "degraded"
    assert data["sources_with_errors"] == 1
    assert data["recent_error_count"] >= 1
    error = data["recent_errors"][0]
    assert error["path"].endswith("a.py")
    assert error["error_code"] == "RuntimeError"
    assert "simulated parser crash" in error["error_message"]
    assert error["occurred_at"]
    row = data["sources"][0]
    assert row["index_state"] == "errors"
    assert row["last_error_detail"]["error_code"] == "RuntimeError"
    assert any(p["scope"] == "file" for p in data["problems"])

    monkeypatch.setattr(status_cli.console, "_width", 200)
    human = runner.invoke(app, ["status"])
    assert "simulatedparsercrash" in _flat(human.output)
    assert "degraded" in human.output


def test_retry_jobs_are_separated_from_queued(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = _setup(runner, tmp_path, monkeypatch)
    assert runner.invoke(app, ["index"]).exit_code == 0
    source_id = _source_id()
    with AppContext.bootstrap() as ctx:
        conn = ctx.project_conn(paths.project_id_for_path(source_dir), control_plane=True)
        file_ids = [f.id for f in files_repo.list_by_source(conn, source_id)]
        jobs_repo.enqueue(conn, source_id=source_id, file_id=file_ids[0])
        jobs_repo.enqueue(conn, source_id=source_id, file_id=file_ids[1])
        job = jobs_repo.claim_next(conn)
        assert job is not None
        jobs_repo.fail_with_backoff(
            conn,
            job.id,
            error_code="E",
            error_message="later",
            next_attempt_at="2999-01-01T00:00:00+00:00",
            permanent=False,
        )

    data = _status_json(runner)
    assert data["queue"]["queued"] == 1
    assert data["queue"]["retry"] == 1
    assert data["queue"]["next_retry_at"] == "2999-01-01T00:00:00+00:00"
    assert data["totals"]["queue_depth"] == 2  # legacy semantics unchanged
    assert data["sources"][0]["index_state"] == "retrying"
    # Pending work with no indexer is informational, never "stalled".
    assert data["indexer"]["state"] == "idle"


def test_foreground_index_visible_to_separate_status(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates another process mid-run: holds index.lock and writes a
    live progress snapshot, then asks ``ragmonk status``.
    """
    _setup(runner, tmp_path, monkeypatch)
    source_id = _source_id()
    lock = RunLock(
        paths.locks_dir(ragmonk_home) / "index.lock", operation="index", source_id=source_id
    )
    lock.acquire()
    try:
        with progress.track(ragmonk_home, operation="index", source_total=3) as tracker:
            tracker.begin_source(source_id, 2)
            tracker.stage("embeddings")
            data = _status_json(runner)
            monkeypatch.setattr(status_cli.console, "_width", 200)
            human = runner.invoke(app, ["status"]).output
    finally:
        lock.release()

    indexer = data["indexer"]
    assert indexer["state"] == "running"
    assert indexer["lock_state"] == "held"
    assert indexer["pid"] == os.getpid()
    assert indexer["current_source_id"] == source_id
    assert (indexer["source_position"], indexer["source_total"]) == (2, 3)
    assert indexer["stage"] == "embeddings"
    assert data["sources"][0]["index_state"] == "indexing"
    assert "embeddings" in human
    assert "(2/3)" in human


def test_stale_heartbeat_with_held_lock_is_stalled(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(runner, tmp_path, monkeypatch)
    source_id = _source_id()
    old = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    progress.write_progress(
        ragmonk_home,
        progress.IndexProgress(
            running=True,
            pid=os.getpid(),
            operation="index",
            source_id=source_id,
            stage="processing",
            started_at=old,
            updated_at=old,
        ),
    )
    lock = RunLock(paths.locks_dir(ragmonk_home) / "index.lock", operation="index")
    lock.acquire()
    try:
        data = _status_json(runner)
    finally:
        lock.release()
    assert data["indexer"]["state"] == "stalled"
    assert data["sources"][0]["index_state"] == "stalled"
    assert data["health"]["status"] == "failed"


def test_stale_heartbeat_without_lock_is_historical(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(runner, tmp_path, monkeypatch)
    old = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    progress.write_progress(
        ragmonk_home,
        progress.IndexProgress(
            running=True, pid=999_999, operation="index", started_at=old, updated_at=old
        ),
    )
    monkeypatch.setattr("ragmonk.service.pid.is_process_alive", lambda _pid: False)
    data = _status_json(runner)
    assert data["indexer"]["state"] == "crashed"
    assert data["sources"][0]["index_state"] != "stalled"
    assert data["sources"][0]["index_state"] != "indexing"


def test_errors_flag_filters_healthy_sources(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = _setup(runner, tmp_path, monkeypatch)
    assert runner.invoke(app, ["index"]).exit_code == 0
    result = runner.invoke(app, ["status", "--errors"])
    assert result.exit_code == 0, result.output
    assert "No problems found" in result.output
    assert _source_id() not in result.output

    verbose = runner.invoke(app, ["status", "--verbose"])
    assert verbose.exit_code == 0, verbose.output
    assert "Lock:" in verbose.output
    assert "Oldest pending" in verbose.output
    assert source_dir.exists()


def test_watch_exits_cleanly_on_ctrl_c(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(runner, tmp_path, monkeypatch)
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(status_cli.time, "sleep", fake_sleep)
    result = runner.invoke(app, ["status", "--watch", "--interval", "0.1"])
    assert result.exit_code == 0, result.output
    assert ticks["n"] == 2
    assert "Throughput" in result.output

    both = runner.invoke(app, ["status", "--watch", "--json"])
    assert both.exit_code != 0


def test_watch_deltas_and_throughput() -> None:
    first = status_cli._Sample(at=10.0, indexed=100, failed=1, retry=2, depth=50)
    assert status_cli.watch_deltas(None, first)["rate"] is None
    second = status_cli._Sample(at=12.0, indexed=110, failed=1, retry=1, depth=40)
    deltas = status_cli.watch_deltas(first, second)
    assert deltas["indexed"] == 10
    assert deltas["depth"] == -10
    assert deltas["rate"] == pytest.approx(5.0)
    # Counter reset (rebuild): never a negative throughput.
    reset = status_cli._Sample(at=14.0, indexed=0, failed=0, retry=0, depth=200)
    assert status_cli.watch_deltas(second, reset)["rate"] is None
    static = status_cli._Sample(at=16.0, indexed=0, failed=0, retry=0, depth=200)
    assert status_cli.watch_deltas(reset, static)["rate"] == 0.0
