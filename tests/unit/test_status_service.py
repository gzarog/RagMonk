"""Status observability V1: lock/progress/queue aggregation, index-state
precedence, stall detection and the health verdict.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext, RunLock
from ragmonk.service import progress
from ragmonk.service.status_service import (
    derive_health,
    derive_index_state,
    derive_problems,
    indexer_state,
)


@pytest.fixture
def ctx(ragmonk_home: Path) -> Iterator[AppContext]:
    with AppContext.bootstrap() as context:
        yield context


@pytest.fixture
def held_lock(ragmonk_home: Path) -> Iterator[RunLock]:
    lock = RunLock(
        paths.locks_dir(ragmonk_home) / "index.lock", operation="index", source_id="src_a"
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def _write_snapshot(home: Path, *, age_seconds: float, running: bool = True, **extra: Any) -> None:
    updated = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    fields: dict[str, Any] = {
        "running": running,
        "pid": os.getpid(),
        "operation": "index",
        "outcome": "running" if running else "completed",
        "started_at": updated,
        "updated_at": updated,
        "source_id": "src_a",
        "source_position": 2,
        "source_total": 3,
        "stage": "processing",
    }
    fields.update(extra)
    progress.write_progress(home, progress.IndexProgress(**fields))


# -- indexer_state --------------------------------------------------------


def test_indexer_idle_when_lock_free_and_no_progress(ctx: AppContext) -> None:
    info = indexer_state(ctx)
    assert info["state"] == "idle"
    assert info["lock_state"] == "free"
    assert info["run"] is None and info["last_run"] is None


def test_held_lock_is_visible_with_owner_metadata(ctx: AppContext, held_lock: RunLock) -> None:
    info = indexer_state(ctx)
    assert info["state"] == "running"
    assert info["lock_state"] == "held"
    assert info["pid"] == os.getpid()
    assert info["operation"] == "index"
    assert info["current_source_id"] == "src_a"
    assert info["running_for_seconds"] is not None


def test_fresh_heartbeat_reports_source_position_and_stage(
    ctx: AppContext, held_lock: RunLock
) -> None:
    _write_snapshot(ctx.home, age_seconds=1)
    info = indexer_state(ctx)
    assert info["state"] == "running"
    assert info["current_source_id"] == "src_a"
    assert (info["source_position"], info["source_total"]) == (2, 3)
    assert info["stage"] == "processing"
    assert info["last_activity_age_seconds"] is not None
    assert info["last_activity_age_seconds"] < 60


def test_stale_heartbeat_with_held_lock_is_stalled(ctx: AppContext, held_lock: RunLock) -> None:
    _write_snapshot(ctx.home, age_seconds=ctx.config.indexing.status_stall_threshold_seconds + 30)
    info = indexer_state(ctx)
    assert info["state"] == "stalled"
    assert "no progress heartbeat" in info["stall_reason"]


def test_stale_running_snapshot_without_lock_or_process_is_crashed(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(ctx.home, age_seconds=10_000, pid=999_999)
    monkeypatch.setattr("ragmonk.service.pid.is_process_alive", lambda _pid: False)
    info = indexer_state(ctx)
    # Historical information, not active indexing -- and never "stalled".
    assert info["state"] == "crashed"
    assert info["run"] is None
    assert info["last_run"]["outcome"] == "crashed"
    assert info["current_source_id"] is None


def test_completed_snapshot_is_last_run_not_activity(ctx: AppContext) -> None:
    _write_snapshot(ctx.home, age_seconds=10_000, running=False)
    info = indexer_state(ctx)
    assert info["state"] == "idle"
    assert info["last_run"]["outcome"] == "completed"


def test_malformed_lock_metadata_does_not_crash(ctx: AppContext, held_lock: RunLock) -> None:
    lock_path = paths.locks_dir(ctx.home) / "index.lock"
    with lock_path.open("r+b") as fh:
        fh.seek(1)
        fh.write(b"\xff{garbage")
    info = indexer_state(ctx)
    assert info["lock_state"] == "held"
    assert info["state"] == "running"


# -- derive_index_state ---------------------------------------------------


def _state(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "access_state": "online",
        "queue": {"queued": 0, "processing": 0, "retry": 0, "failed": 0},
        "failed_files": 0,
        "last_error": None,
        "last_scan_at": None,
        "indexer": {"state": "idle", "current_source_id": None},
        "source_id": "src_a",
    }
    kwargs.update(overrides)
    return derive_index_state(**kwargs)


def test_reachable_source_without_work_is_not_indexing() -> None:
    assert _state() == "idle"
    assert _state(last_scan_at="2026-01-01T00:00:00+00:00") == "completed"


def test_index_state_precedence() -> None:
    running = {"state": "running", "current_source_id": "src_a"}
    stalled = {"state": "stalled", "current_source_id": "src_a"}
    busy_queue = {"queued": 5, "processing": 1, "retry": 2, "failed": 1}
    assert _state(access_state="offline", indexer=stalled) == "offline"
    assert _state(indexer=stalled, queue=busy_queue) == "stalled"
    assert _state(indexer=running, queue=busy_queue) == "indexing"
    assert _state(queue=busy_queue) == "retrying"
    assert _state(queue={"queued": 1, "retry": 0}, failed_files=2) == "errors"
    assert _state(last_error="linking failed: x") == "errors"
    assert _state(queue={"queued": 3, "retry": 0}) == "waiting"


def test_running_indexer_on_another_source_does_not_mark_this_one_indexing() -> None:
    other = {"state": "running", "current_source_id": "src_b"}
    assert _state(indexer=other, queue={"queued": 4, "retry": 0}) == "waiting"


# -- problems / health ----------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "src_a",
        "enabled": True,
        "access_state": "online",
        "last_error": None,
        "counts": {"indexed": 10},
        "queue": {"retry": 0, "failed": 0},
    }
    row.update(overrides)
    return row


_IDLE = {"state": "idle", "last_run": None}


def test_healthy_when_no_problems() -> None:
    problems = derive_problems([_row()], _IDLE, {"type": "local"}, {"depth": 0})
    assert problems == []
    assert derive_health(problems) == "healthy"


def test_one_failed_file_is_degraded_not_failed() -> None:
    problems = derive_problems(
        [_row(counts={"indexed": 10, "failed": 1})], _IDLE, {"type": "local"}, {"depth": 0}
    )
    assert derive_health(problems) == "degraded"
    assert problems[0]["scope"] == "file"
    assert problems[0]["message"] == "1 file(s) failed"


def test_backend_unreachable_is_failed() -> None:
    backend = {"type": "server/opensearch", "error": "server unreachable: refused"}
    problems = derive_problems([_row()], _IDLE, backend, {"depth": 0})
    assert derive_health(problems) == "failed"
    assert problems[0]["scope"] == "backend"


def test_stalled_indexer_is_failed() -> None:
    indexer = {"state": "stalled", "stall_reason": "no progress heartbeat for 300s"}
    problems = derive_problems([_row()], indexer, {"type": "local"}, {"depth": 0})
    assert derive_health(problems) == "failed"
    assert "300s" in problems[0]["message"]


def test_pending_queue_alone_is_informational() -> None:
    problems = derive_problems([_row()], _IDLE, {"type": "local"}, {"depth": 7})
    assert [p["severity"] for p in problems] == ["info"]
    assert derive_health(problems) == "healthy"


def test_offline_sources() -> None:
    offline = _row(access_state="offline", last_error="root missing")
    online = _row(id="src_b")
    partial = derive_problems([offline, online], _IDLE, {"type": "local"}, {"depth": 0})
    assert derive_health(partial) == "degraded"
    everything = derive_problems([offline], _IDLE, {"type": "local"}, {"depth": 0})
    assert derive_health(everything) == "failed"


def test_interrupted_last_run_is_only_degraded() -> None:
    indexer = {
        "state": "idle",
        "last_run": {"outcome": "failed", "operation": "index", "error": "KeyboardInterrupt: "},
    }
    problems = derive_problems([_row()], indexer, {"type": "local"}, {"depth": 0})
    assert derive_health(problems) == "degraded"
