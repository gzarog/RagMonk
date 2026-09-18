"""The daemon's heartbeat/health snapshot: what ``ragmonk daemon
status``/``doctor`` read to report on a running daemon *without* talking
to the daemon process itself (no local socket/RPC in this phase -- see
the blueprint's scope for Phase 7). The daemon (``service/daemon.py``)
overwrites this file after every reconciliation pass and on every
watcher-triggered indexing pass.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.core import paths


@dataclass(frozen=True)
class SourceWatchStatus:
    source_id: str
    path: str
    source_type: str  # "local" | "network"
    online: bool
    last_pass_at: str | None = None


@dataclass(frozen=True)
class DaemonHealth:
    started_at: str
    updated_at: str
    last_reconciliation_at: str | None = None
    sources: list[SourceWatchStatus] = field(default_factory=list)


def write_health(home: Path, health: DaemonHealth) -> None:
    path = paths.daemon_health_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(health)
    # Write-then-rename: `daemon status` reads this file from an
    # unrelated process at an arbitrary moment, and a torn write (partial
    # JSON) would otherwise be a real, if narrow, race -- os.replace is
    # atomic on both POSIX and Windows. The daemon's worker thread and its
    # separate reconciliation thread can both call this concurrently
    # (see service/daemon.py) -- a *fixed* tmp filename let one thread's
    # rename consume the other's tmp file first, so the second thread's
    # own os.replace then raised FileNotFoundError. Suffix with pid+tid so
    # concurrent writers never share a tmp path.
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, path)


def read_health(home: Path) -> DaemonHealth | None:
    path = paths.daemon_health_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DaemonHealth(
            started_at=data["started_at"],
            updated_at=data["updated_at"],
            last_reconciliation_at=data.get("last_reconciliation_at"),
            sources=[SourceWatchStatus(**s) for s in data.get("sources", [])],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
