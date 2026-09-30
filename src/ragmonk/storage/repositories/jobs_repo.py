"""Durable job queue backing ``index_jobs`` in a project's ``knowledge.db``.

A job left ``PROCESSING`` means the process died mid-work; ``recover_stuck``
must run before a new run claims anything so that work is never silently
lost across a crash.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

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


def queue_stats(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, Any]:
    """Per-state breakdown of ``index_jobs`` for ``ragmonk status``.

    Unlike :func:`queue_depth` (QUEUED + RETRY lumped together), this
    keeps each state separate so status can say *why* a queue is non-
    empty. ``depth`` preserves ``queue_depth``'s own semantics. Optional
    ``source_id`` scopes every figure to one source.
    """
    where = " WHERE source_id = ?" if source_id is not None else ""
    params: tuple[Any, ...] = (source_id,) if source_id is not None else ()
    counts = {status.value: 0 for status in JobStatus}
    for row in conn.execute(
        f"SELECT status, COUNT(*) AS n FROM index_jobs{where} GROUP BY status", params
    ):
        counts[row["status"]] = int(row["n"])

    scope = " AND source_id = ?" if source_id is not None else ""
    pending = (JobStatus.QUEUED.value, JobStatus.RETRY.value)
    row = conn.execute(
        f"""
        SELECT
            (SELECT MIN(created_at) FROM index_jobs
             WHERE status IN (?, ?){scope}) AS oldest_pending,
            (SELECT MIN(started_at) FROM index_jobs
             WHERE status = ?{scope}) AS oldest_processing,
            (SELECT MIN(next_attempt_at) FROM index_jobs
             WHERE status = ? AND next_attempt_at IS NOT NULL{scope}) AS next_retry,
            (SELECT MAX(attempt_count) FROM index_jobs
             WHERE status IN (?, ?){scope}) AS max_attempts
        """,
        (
            *pending,
            *params,
            JobStatus.PROCESSING.value,
            *params,
            JobStatus.RETRY.value,
            *params,
            JobStatus.RETRY.value,
            JobStatus.FAILED.value,
            *params,
        ),
    ).fetchone()
    latest = conn.execute(
        f"""
        SELECT file_id, status, error_code, error_message FROM index_jobs
        WHERE status IN (?, ?) AND error_code IS NOT NULL{scope}
        ORDER BY COALESCE(completed_at, started_at, created_at) DESC
        LIMIT 1
        """,
        (JobStatus.RETRY.value, JobStatus.FAILED.value, *params),
    ).fetchone()
    queued = counts[JobStatus.QUEUED.value]
    retry_count = counts[JobStatus.RETRY.value]
    return {
        "queued": queued,
        "processing": counts[JobStatus.PROCESSING.value],
        "retry": retry_count,
        "failed": counts[JobStatus.FAILED.value],
        "completed": counts[JobStatus.COMPLETED.value],
        "depth": queued + retry_count,
        "oldest_pending_created_at": row["oldest_pending"],
        "oldest_processing_started_at": row["oldest_processing"],
        "next_retry_at": row["next_retry"],
        "max_attempt_count": int(row["max_attempts"] or 0),
        # Most recent retry/failed job error: transient failures are
        # otherwise invisible until they exhaust their retries.
        "latest_job_error": (
            {
                "file_id": latest["file_id"],
                "status": latest["status"],
                "error_code": latest["error_code"],
                "error_message": latest["error_message"],
            }
            if latest is not None
            else None
        ),
    }


def get(conn: sqlite3.Connection, job_id: str) -> IndexJob | None:
    row = conn.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None
