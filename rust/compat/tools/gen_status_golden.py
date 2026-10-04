"""Generate RUST-11 status-verdict golden fixtures from the Python reference.

    python rust/compat/tools/gen_status_golden.py

Runs the reference's ``service/status_service.py`` verdicts over fixed
scenarios:

* ``indexer``: ``indexer_state`` for combinations of lock state, lock
  owner, progress snapshot, process liveness and clock. ``inspect_lock``,
  ``read_progress`` and ``is_process_alive`` are replaced by the scenario's
  values.
* ``index_state``: ``derive_index_state`` cases.
* ``problems``: ``derive_problems`` + ``derive_health`` cases.
* ``merge``: ``_merge_queue_stats`` cases.

Writes rust/compat/golden/status.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from ragmonk.core.lifecycle import LockStatus
from ragmonk.service import status_service
from ragmonk.service.progress import IndexProgress

OUT = Path(__file__).resolve().parents[1] / "golden" / "status.json"
NOW = "2026-01-02T03:04:05.000000+00:00"
FRESH = "2026-01-02T03:03:55.250000+00:00"  # 9.75 s before NOW
OLD = "2026-01-02T02:54:05.000000+00:00"  # 600 s before NOW
OWNER = {
    "pid": 4242,
    "operation": "index",
    "source_id": "src-a",
    "hostname": "host1",
    "acquired_at": "2026-01-02T03:00:00+00:00",
}


def snap(**kw) -> dict:
    base = {
        "running": True,
        "pid": 4242,
        "operation": "index",
        "outcome": "running",
        "started_at": "2026-01-02T03:00:01+00:00",
        "updated_at": FRESH,
        "source_id": "src-a",
        "source_position": 1,
        "source_total": 2,
        "stage": "process",
        "scanned": 10,
        "queued": 4,
        "indexed": 6,
    }
    base.update(kw)
    return base


INDEXER = [
    {"name": "nothing", "lock": "free", "owner": None, "snapshot": None, "alive": []},
    {"name": "held_no_snapshot", "lock": "held", "owner": OWNER, "snapshot": None, "alive": []},
    {"name": "held_fresh", "lock": "held", "owner": OWNER, "snapshot": snap(), "alive": [4242]},
    {
        "name": "held_stale",
        "lock": "held",
        "owner": OWNER,
        "snapshot": snap(updated_at=OLD),
        "alive": [4242],
    },
    {
        "name": "free_alive_between_sources",
        "lock": "free",
        "owner": None,
        "snapshot": snap(),
        "alive": [4242],
    },
    {"name": "crashed", "lock": "free", "owner": None, "snapshot": snap(), "alive": []},
    {
        "name": "held_by_other_pid_dead_snapshot",
        "lock": "held",
        "owner": {**OWNER, "pid": 99},
        "snapshot": snap(),
        "alive": [],
    },
    {
        "name": "held_owner_without_pid",
        "lock": "held",
        "owner": {k: v for k, v in OWNER.items() if k != "pid"},
        "snapshot": snap(),
        "alive": [],
    },
    {
        "name": "completed",
        "lock": "free",
        "owner": None,
        "snapshot": snap(running=False, outcome="completed", completed_at=FRESH, stage="done"),
        "alive": [],
    },
    {
        "name": "failed",
        "lock": "free",
        "owner": None,
        "snapshot": snap(running=False, outcome="failed", error="ValueError: boom"),
        "alive": [],
    },
    {"name": "unknown_lock", "lock": "unknown", "owner": None, "snapshot": None, "alive": []},
    {
        "name": "naive_timestamp",
        "lock": "free",
        "owner": None,
        "snapshot": snap(updated_at="2026-01-02T02:59:00"),
        "alive": [4242],
    },
]


def run_indexer(case: dict) -> dict:
    snapshot = IndexProgress(**case["snapshot"]) if case["snapshot"] else None
    status_service.inspect_lock = lambda _p: LockStatus(case["lock"], case["owner"])
    status_service.progress.read_progress = lambda _h: snapshot
    alive = set(case["alive"])
    status_service.pid.is_process_alive = lambda p: p in alive
    ctx = SimpleNamespace(
        home=Path("/nonexistent"),
        config=SimpleNamespace(indexing=SimpleNamespace(status_stall_threshold_seconds=120)),
    )
    return status_service.indexer_state(ctx, now=datetime.fromisoformat(NOW))


Q0 = {"queued": 0, "processing": 0, "retry": 0, "failed": 0}
IDLE = {"state": "idle", "current_source_id": None}
RUNNING = {"state": "running", "current_source_id": "s"}
STALLED = {"state": "stalled", "current_source_id": "s"}
INDEX_STATE = [
    dict(
        access_state="offline",
        queue=Q0,
        failed_files=0,
        last_error=None,
        last_scan_at=None,
        indexer=RUNNING,
    ),
    dict(
        access_state="online",
        queue=Q0,
        failed_files=0,
        last_error=None,
        last_scan_at=None,
        indexer=STALLED,
    ),
    dict(
        access_state="online",
        queue={**Q0, "retry": 2},
        failed_files=1,
        last_error=None,
        last_scan_at="t",
        indexer=RUNNING,
    ),
    dict(
        access_state="online",
        queue={**Q0, "retry": 2},
        failed_files=1,
        last_error=None,
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue=Q0,
        failed_files=1,
        last_error=None,
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue={**Q0, "failed": 1},
        failed_files=0,
        last_error=None,
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue=Q0,
        failed_files=0,
        last_error="disk",
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue=Q0,
        failed_files=0,
        last_error="",
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue={**Q0, "queued": 3},
        failed_files=0,
        last_error=None,
        last_scan_at=None,
        indexer=IDLE,
    ),
    dict(
        access_state="online",
        queue={**Q0, "processing": 1},
        failed_files=0,
        last_error=None,
        last_scan_at=None,
        indexer={"state": "running", "current_source_id": "other"},
    ),
    dict(
        access_state="online",
        queue=Q0,
        failed_files=0,
        last_error=None,
        last_scan_at="t",
        indexer=IDLE,
    ),
    dict(
        access_state="disabled",
        queue=Q0,
        failed_files=0,
        last_error=None,
        last_scan_at=None,
        indexer=IDLE,
    ),
]


def src(id_: str, **kw) -> dict:
    row = {
        "id": id_,
        "enabled": True,
        "access_state": "online",
        "last_error": None,
        "counts": {"indexed": 3},
        "queue": {**Q0, "latest_job_error": None},
    }
    row.update(kw)
    return row


PROBLEMS = [
    {"sources": [src("a")], "indexer": IDLE, "backend": {"type": "local"}, "queue": {"depth": 0}},
    {
        "sources": [src("a", access_state="offline", last_error="gone"), src("b")],
        "indexer": {
            "state": "stalled",
            "stall_reason": "no progress heartbeat for 600s",
            "current_source_id": "b",
        },
        "backend": {"type": "server/opensearch", "error": "server unreachable: refused"},
        "queue": {"depth": 0},
    },
    {
        "sources": [
            src("a", access_state="offline"),
            src("b", access_state="offline"),
            src("c", enabled=False, access_state="disabled"),
        ],
        "indexer": {
            "state": "crashed",
            "last_run": {"operation": None, "pid": 77, "outcome": "crashed"},
        },
        "backend": {"type": "local"},
        "queue": {"depth": 5},
    },
    {
        "sources": [
            src(
                "a",
                last_error="permission denied",
                counts={"failed": 2},
                queue={
                    **Q0,
                    "retry": 3,
                    "latest_job_error": {"error_code": "io", "error_message": "busy"},
                },
            ),
            src("b", queue={**Q0, "retry": 1, "latest_job_error": None}),
        ],
        "indexer": {
            "state": "idle",
            "last_run": {
                "operation": "rebuild",
                "outcome": "failed",
                "error": "KeyboardInterrupt: ",
            },
        },
        "backend": {"type": "local"},
        "queue": {"depth": 4},
    },
    {
        "sources": [src("a")],
        "indexer": {
            "state": "idle",
            "last_run": {"operation": "index", "outcome": "failed", "error": None},
        },
        "backend": {"type": "local"},
        "queue": {"depth": 2},
    },
    {"sources": [], "indexer": IDLE, "backend": {}, "queue": {"depth": 1}},
]

MERGE = [
    [],
    [
        {
            "queued": 1,
            "processing": 0,
            "retry": 2,
            "failed": 1,
            "completed": 4,
            "depth": 3,
            "oldest_pending_created_at": "2026-01-02",
            "oldest_processing_started_at": None,
            "next_retry_at": "2026-01-05",
            "max_attempt_count": 2,
            "latest_job_error": None,
        },
        {
            "queued": 0,
            "processing": 1,
            "retry": 0,
            "failed": 0,
            "completed": 1,
            "depth": 1,
            "oldest_pending_created_at": "2026-01-01",
            "oldest_processing_started_at": "2026-01-03",
            "next_retry_at": "",
            "max_attempt_count": 5,
            "latest_job_error": {"error_code": "x", "error_message": "y"},
        },
    ],
]


def main() -> None:
    payload = {
        "now": NOW,
        "threshold": 120,
        "indexer": [
            {**case, "snapshot": case["snapshot"], "expected": run_indexer(case)}
            for case in INDEXER
        ],
        "index_state": [
            {"input": c, "expected": status_service.derive_index_state(**c, source_id="s")}
            for c in INDEX_STATE
        ],
        "problems": [
            {
                "input": c,
                "problems": (
                    p := status_service.derive_problems(
                        c["sources"], c["indexer"], c["backend"], c["queue"]
                    )
                ),
                "health": status_service.derive_health(p),
            }
            for c in PROBLEMS
        ],
        "merge": [{"input": c, "expected": status_service._merge_queue_stats(c)} for c in MERGE],
        "progress_fields": list(asdict(IndexProgress()).keys()),
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    _ = UTC
    main()
