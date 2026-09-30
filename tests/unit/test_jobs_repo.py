from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ragmonk.core.models import FileKind, FileRecord, FileStatus, JobStatus
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo, jobs_repo
from ragmonk.storage.sqlite import connect


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def _insert_file(conn: sqlite3.Connection, file_id: str = "file-1") -> FileRecord:
    record = FileRecord(
        id=file_id,
        source_id="src-1",
        path=f"/root/{file_id}.py",
        kind=FileKind.CODE,
        size=1,
        mtime=1.0,
        status=FileStatus.QUEUED,
        created_at="now",
        updated_at="now",
    )
    files_repo.insert(conn, record)
    return record


def test_enqueue_and_claim_transitions_to_processing(conn: sqlite3.Connection) -> None:
    _insert_file(conn)
    job_id = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    claimed = jobs_repo.claim_next(conn)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status is JobStatus.PROCESSING

    stored = jobs_repo.get(conn, job_id)
    assert stored is not None
    assert stored.status is JobStatus.PROCESSING


def test_claim_next_returns_none_when_queue_empty(conn: sqlite3.Connection) -> None:
    assert jobs_repo.claim_next(conn) is None


def test_complete_marks_job_completed(conn: sqlite3.Connection) -> None:
    _insert_file(conn)
    job_id = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    jobs_repo.claim_next(conn)
    jobs_repo.complete(conn, job_id)
    stored = jobs_repo.get(conn, job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED


def test_fail_with_backoff_permanent_marks_failed(conn: sqlite3.Connection) -> None:
    _insert_file(conn)
    job_id = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    jobs_repo.claim_next(conn)
    jobs_repo.fail_with_backoff(
        conn,
        job_id,
        error_code="ValueError",
        error_message="boom",
        next_attempt_at=None,
        permanent=True,
    )
    stored = jobs_repo.get(conn, job_id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.attempt_count == 1


def test_fail_with_backoff_transient_marks_retry(conn: sqlite3.Connection) -> None:
    _insert_file(conn)
    job_id = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    jobs_repo.claim_next(conn)
    jobs_repo.fail_with_backoff(
        conn,
        job_id,
        error_code="TimeoutError",
        error_message="slow",
        next_attempt_at="2999-01-01T00:00:00+00:00",
        permanent=False,
    )
    stored = jobs_repo.get(conn, job_id)
    assert stored is not None
    assert stored.status is JobStatus.RETRY


def test_recover_stuck_requeues_processing_jobs(conn: sqlite3.Connection) -> None:
    """Simulates a crash: a job left PROCESSING must come back as QUEUED on
    the next startup so its work is never silently lost."""
    _insert_file(conn)
    job_id = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    jobs_repo.claim_next(conn)
    assert jobs_repo.get(conn, job_id).status is JobStatus.PROCESSING  # type: ignore[union-attr]

    recovered = jobs_repo.recover_stuck(conn)
    assert recovered == 1
    assert jobs_repo.get(conn, job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_queue_depth_counts_queued_and_retry(conn: sqlite3.Connection) -> None:
    _insert_file(conn, "file-1")
    _insert_file(conn, "file-2")
    jobs_repo.enqueue(conn, source_id="src-1", file_id="file-1")
    job2 = jobs_repo.enqueue(conn, source_id="src-1", file_id="file-2")
    jobs_repo.fail_with_backoff(
        conn, job2, error_code="E", error_message="m", next_attempt_at=None, permanent=False
    )
    assert jobs_repo.queue_depth(conn) == 2


# -- queue_stats (status observability V1) ------------------------------


def test_queue_stats_empty_queue(conn: sqlite3.Connection) -> None:
    stats = jobs_repo.queue_stats(conn)
    assert stats["queued"] == stats["processing"] == stats["retry"] == stats["failed"] == 0
    assert stats["depth"] == 0
    assert stats["oldest_pending_created_at"] is None
    assert stats["next_retry_at"] is None
    assert stats["max_attempt_count"] == 0


def test_queue_stats_separates_queued_retry_processing_failed(conn: sqlite3.Connection) -> None:
    for file_id in ("f1", "f2", "f3", "f4", "f5"):
        _insert_file(conn, file_id)
        jobs_repo.enqueue(conn, source_id="src-1", file_id=file_id)
    processing = jobs_repo.claim_next(conn)
    retrying = jobs_repo.claim_next(conn)
    failing = jobs_repo.claim_next(conn)
    assert processing is not None and retrying is not None and failing is not None
    jobs_repo.fail_with_backoff(
        conn,
        retrying.id,
        error_code="E",
        error_message="try again",
        next_attempt_at="2999-01-01T00:00:00+00:00",
        permanent=False,
    )
    jobs_repo.fail_with_backoff(
        conn, failing.id, error_code="E", error_message="dead", next_attempt_at=None, permanent=True
    )

    stats = jobs_repo.queue_stats(conn)
    assert stats["queued"] == 2
    assert stats["processing"] == 1
    assert stats["retry"] == 1
    assert stats["failed"] == 1
    # Backward compatible with queue_depth: queued + retry.
    assert stats["depth"] == 3 == jobs_repo.queue_depth(conn)
    assert stats["next_retry_at"] == "2999-01-01T00:00:00+00:00"
    assert stats["oldest_pending_created_at"] is not None
    assert stats["oldest_processing_started_at"] is not None
    assert stats["max_attempt_count"] == 1


def test_queue_stats_source_scoped(conn: sqlite3.Connection) -> None:
    _insert_file(conn, "a")
    _insert_file(conn, "b")
    jobs_repo.enqueue(conn, source_id="src-1", file_id="a")
    jobs_repo.enqueue(conn, source_id="src-2", file_id="b")

    assert jobs_repo.queue_stats(conn)["queued"] == 2
    assert jobs_repo.queue_stats(conn, "src-1")["queued"] == 1
    assert jobs_repo.queue_stats(conn, "src-2")["depth"] == 1
    assert jobs_repo.queue_stats(conn, "missing")["depth"] == 0
