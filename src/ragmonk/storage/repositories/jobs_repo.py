"""Durable job queue backing ``index_jobs`` in a project's ``knowledge.db``.

A job left ``PROCESSING`` means the process died mid-work; ``recover_stuck``
must run before a new run claims anything so that work is never silently
lost across a crash.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from ragmonk.core.models import IndexJob, JobStatus, JobType
from ragmonk.storage.sqlite import transaction


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: sqlite3.Row) -> IndexJob:
    return IndexJob(
        id=row["id"],
        source_id=row["source_id"],
        file_id=row["file_id"],
        job_type=JobType(row["job_type"]),
        status=JobStatus(row["status"]),
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def enqueue(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    file_id: str,
    job_type: JobType = JobType.INDEX_FILE,
    priority: int = 0,
) -> str:
    job_id = uuid.uuid4().hex
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO index_jobs (
                id, source_id, file_id, job_type, status, priority,
                attempt_count, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
            """,
            (job_id, source_id, file_id, job_type.value, JobStatus.QUEUED.value, priority, _now()),
        )
    return job_id


def claim_next(conn: sqlite3.Connection) -> IndexJob | None:
    now = _now()
    with transaction(conn):
        row = conn.execute(
            """
            SELECT * FROM index_jobs
            WHERE status IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (JobStatus.QUEUED.value, JobStatus.RETRY.value, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE index_jobs SET status = ?, started_at = ? WHERE id = ?",
            (JobStatus.PROCESSING.value, now, row["id"]),
        )
    job = _row_to_job(row)
    return job.model_copy(update={"status": JobStatus.PROCESSING, "started_at": now})


def complete(conn: sqlite3.Connection, job_id: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE index_jobs SET status = ?, completed_at = ? WHERE id = ?",
            (JobStatus.COMPLETED.value, _now(), job_id),
        )


def fail_with_backoff(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    error_code: str,
    error_message: str,
    next_attempt_at: str | None,
    permanent: bool,
) -> None:
    status = JobStatus.FAILED if permanent else JobStatus.RETRY
    with transaction(conn):
        conn.execute(
            """
            UPDATE index_jobs
            SET status = ?, attempt_count = attempt_count + 1,
                next_attempt_at = ?, error_code = ?, error_message = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                status.value,
                next_attempt_at,
                error_code,
                error_message,
                _now() if permanent else None,
                job_id,
            ),
        )


def requeue_retry(conn: sqlite3.Connection, job_id: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE index_jobs SET status = ? WHERE id = ?", (JobStatus.QUEUED.value, job_id)
        )


def recover_stuck(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE index_jobs SET status = ?, started_at = NULL WHERE status = ?",
            (JobStatus.QUEUED.value, JobStatus.PROCESSING.value),
        )
        return cursor.rowcount


def queue_depth(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM index_jobs WHERE status IN (?, ?)",
        (JobStatus.QUEUED.value, JobStatus.RETRY.value),
    ).fetchone()
    return int(row["n"])


def get(conn: sqlite3.Connection, job_id: str) -> IndexJob | None:
    row = conn.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None
