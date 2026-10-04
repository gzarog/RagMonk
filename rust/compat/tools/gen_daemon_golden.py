"""Generate RUST-11 daemon golden fixtures from the Python reference.

    python rust/compat/tools/gen_daemon_golden.py

* ``uptime``: ``cli/daemon._format_uptime`` cases.
* ``pid_files``: raw ``daemon.pid`` contents and what ``read_pid_file``
  makes of them (``null`` = unreadable).
* ``health_files``: the same for ``daemon_health.json`` / ``read_health``.
* ``scheduler``: trigger scripts replayed on ``service/daemon.Daemon``'s
  coalescing (``enqueue_source``, the worker's dequeue plus
  ``_build_scan_request``, ``_settle_pending_state``), with no threads.
  After each step the queue and per-source states are recorded.

Writes rust/compat/golden/daemon.json.
"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from ragmonk.cli.daemon import _format_uptime
from ragmonk.core import paths
from ragmonk.service import health, pid
from ragmonk.service.daemon import Daemon

OUT = Path(__file__).resolve().parents[1] / "golden" / "daemon.json"

UPTIME = [0, 0.9, 1, 59, 59.99, 60, 61, 3599, 3600, 3661, 86399, 86400, 90061, 1_000_000]

PID_FILES = [
    '{"pid": 4242, "started_at": "2026-10-04T10:00:00.000001+00:00"}',
    '{"pid": "17", "started_at": "x"}',
    '{"pid": 9, "started_at": 3}',
    '{"pid": 9.7, "started_at": "x"}',
    '{"pid": 9}',
    '{"started_at": "x"}',
    '{"pid": "nope", "started_at": "x"}',
    '{"pid": 1',
    "",
    "[]",
]

_SRC = {"source_id": "s", "path": "/p", "source_type": "local", "online": True}
HEALTH_FILES = [
    json.dumps({"started_at": "a", "updated_at": "b"}),
    json.dumps(
        {
            "started_at": "a",
            "updated_at": "b",
            "last_reconciliation_at": "c",
            "sources": [_SRC, {**_SRC, "source_id": "t", "online": False, "last_pass_at": "d"}],
        }
    ),
    json.dumps({"started_at": "a", "updated_at": "b", "extra": 1}),
    json.dumps({"started_at": "a", "updated_at": "b", "sources": [{**_SRC, "bogus": 1}]}),
    json.dumps({"started_at": "a", "updated_at": "b", "sources": [{"source_id": "s"}]}),
    json.dumps({"started_at": "a"}),
    "{",
]

# ("trigger", id, reason, paths) | ("start",) | ("finish", id)
MANY = [f"f{i}.py" for i in range(201)]
SCRIPTS = {
    "burst_coalesces": [
        ("trigger", "a", "local_watcher", ["x.py"]),
        ("trigger", "a", "local_watcher", ["y.py"]),
        ("trigger", "a", "reconciliation", []),
        ("start",),
        ("finish", "a"),
    ],
    "followup_goes_to_back": [
        ("trigger", "a", "startup", []),
        ("trigger", "b", "startup", []),
        ("start",),
        ("trigger", "a", "local_watcher", ["x.py"]),
        ("trigger", "a", "local_watcher", ["z.py"]),
        ("finish", "a"),
        ("start",),
        ("finish", "b"),
        ("start",),
        ("finish", "a"),
    ],
    "too_many_paths_is_full": [
        ("trigger", "a", "network_watcher", MANY),
        ("start",),
        ("finish", "a"),
        ("trigger", "a", "network_watcher", MANY[:200]),
        ("start",),
        ("finish", "a"),
    ],
    "contention_requeue": [
        ("trigger", "a", "local_watcher", ["x.py"]),
        ("start_no_request",),
        ("trigger", "a", "lock_contention", []),
        ("finish", "a"),
        ("start",),
        ("finish", "a"),
    ],
    "first_reason_wins": [
        ("trigger", "a", "network_watcher", ["n.txt"]),
        ("trigger", "a", "startup", []),
        ("start",),
        ("finish", "a"),
        ("trigger", "a", "manual", []),
        ("start",),
        ("finish", "a"),
    ],
}


def _bare_daemon() -> Daemon:
    d = Daemon.__new__(Daemon)
    d._queue = queue.Queue()
    d._stop_event = threading.Event()
    d._state_lock = threading.Lock()
    d._pending_state = {}
    d._touched_paths = {}
    d._force_full = {}
    d._trigger_reasons = {}
    return d


def _replay(script: list[tuple]) -> list[dict]:
    d = _bare_daemon()
    steps = []
    for op in script:
        step: dict = {"op": list(op[:3]) if op[0] == "trigger" else list(op)}
        if op[0] == "trigger":
            _, sid, reason, touched = op
            if touched:
                with d._state_lock:
                    d._touched_paths.setdefault(sid, set()).update(touched)
            d.enqueue_source(sid, reason=reason)
            step["paths"] = list(touched)
        elif op[0] in ("start", "start_no_request"):
            sid = d._queue.get_nowait()
            with d._state_lock:
                d._pending_state[sid] = "running"
            step["started"] = sid
            if op[0] == "start":
                req = d._build_scan_request(sid)
                step["request"] = {
                    "source_id": req.source_id,
                    "reason": req.reason,
                    "full": req.full,
                    "changed_paths": sorted(req.changed_paths or []),
                }
        else:
            d._settle_pending_state(op[1])
        step["queue"] = list(d._queue.queue)
        step["states"] = dict(sorted(d._pending_state.items()))
        steps.append(step)
    return steps


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        pid_cases = []
        for text in PID_FILES:
            paths.daemon_pid_path(home).write_text(text, encoding="utf-8")
            info = pid.read_pid_file(home)
            pid_cases.append({"text": text, "parsed": asdict(info) if info else None})
        health_cases = []
        for text in HEALTH_FILES:
            paths.daemon_health_path(home).write_text(text, encoding="utf-8")
            snap = health.read_health(home)
            health_cases.append({"text": text, "parsed": asdict(snap) if snap else None})
    golden = {
        "uptime": [{"seconds": s, "text": _format_uptime(s)} for s in UPTIME],
        "pid_files": pid_cases,
        "health_files": health_cases,
        "scheduler": [{"name": n, "steps": _replay(s)} for n, s in SCRIPTS.items()],
    }
    OUT.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
