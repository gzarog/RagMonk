"""PID-file bookkeeping for ``ragpilot daemon start|stop|restart|status``.

This lets a *separate* CLI invocation (``daemon stop``/``status``, run
later, in a different process) find and signal the actual running daemon
process. It does not, by itself, prevent two daemons from starting --
Phase 1's ``RunLock`` (reused unchanged for each of the daemon's
indexing passes, see ``service/daemon.py``) is what already prevents two
writers from corrupting the same database; this module only adds what a
*different* process needs in order to locate and signal the one that is
running, matching the blueprint's "only add what's needed to find/signal
a different running process" scope.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragpilot.core import paths


@dataclass(frozen=True)
class PidInfo:
    pid: int
    started_at: str


def write_pid_file(home: Path, pid: int) -> PidInfo:
    info = PidInfo(pid=pid, started_at=datetime.now(UTC).isoformat())
    path = paths.daemon_pid_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": info.pid, "started_at": info.started_at}), encoding="utf-8")
    return info


def read_pid_file(home: Path) -> PidInfo | None:
    path = paths.daemon_pid_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PidInfo(pid=int(data["pid"]), started_at=str(data["started_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        # A partially-written or corrupt PID file is treated the same as
        # "no daemon known" rather than raised -- `daemon start` is then
        # free to write a fresh one, and `daemon status`/`stop` report
        # "not running" instead of crashing on stale bookkeeping.
        return None


def remove_pid_file(home: Path) -> None:
    path = paths.daemon_pid_path(home)
    path.unlink(missing_ok=True)


def is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _is_process_alive_windows(pid)
    # Reap ``pid`` first if it happens to be a child of *this* process
    # and has already exited -- plain os.kill(pid, 0) reports a zombie
    # (exited but not yet reaped) as "alive" indefinitely, which is
    # exactly what happens whenever the same process both spawns a
    # daemon and later polls it (a script managing several daemons, or
    # this project's own CliRunner-based tests, which call both
    # ``daemon start`` and ``daemon stop`` in one process). A no-op, not
    # an error, when ``pid`` is not our child.
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by someone else -- still alive.
        return True
    return True


def _is_process_alive_windows(pid: int) -> bool:  # pragma: no cover - exercised only on Windows
    import ctypes

    process_query_limited_information = 0x1000
    windll = ctypes.windll  # type: ignore[attr-defined]
    handle = windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    windll.kernel32.CloseHandle(handle)
    return True


def signal_stop(pid: int) -> None:
    """Asks the daemon at ``pid`` to shut down gracefully.

    POSIX: ``SIGTERM``, which ``service/daemon.py`` installs a handler
    for to stop accepting new triggers and exit its loop cleanly. Windows
    has no portable equivalent signal deliverable to an arbitrary,
    unrelated process; CPython's ``os.kill`` special-cases
    ``signal.SIGTERM`` there to call ``TerminateProcess`` instead, which
    is an immediate hard kill, not a graceful request -- ``daemon
    stop``'s Windows behavior is therefore a hard stop (still safe: each
    indexing pass commits per-file, so nothing is left half-written, see
    ``storage/sqlite.py``'s ``transaction()`` and the crash-recovery
    tests), just not a graceful one.
    """
    os.kill(pid, signal.SIGTERM)


def running_daemon(home: Path) -> PidInfo | None:
    """The recorded daemon, but only if its process is actually still
    alive -- a stale PID file (process died without cleaning up, e.g.
    ``SIGKILL``) is treated as "not running".
    """
    info = read_pid_file(home)
    if info is None or not is_process_alive(info.pid):
        return None
    return info


def stop_and_wait(
    home: Path,
    *,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.1,
    action: str = "proceed",
    error_cls: type[Exception] = RuntimeError,
) -> bool:
    """Stops a running daemon and waits for it to actually exit, so a
    caller about to touch files under ``home`` (``ops/restore.py``'s
    pre-swap step, ``ops/uninstall.py``'s pre-delete step) never races a
    daemon still holding them open. Returns whether a daemon was found
    running at all; raises ``error_cls`` if it doesn't stop in time --
    callers pass their own exception type (e.g. ``DatabaseError``) to
    keep their existing exit-code mapping.
    """
    import time

    info = running_daemon(home)
    if info is None:
        return False
    signal_stop(info.pid)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and is_process_alive(info.pid):
        time.sleep(poll_interval_seconds)
    if is_process_alive(info.pid):
        raise error_cls(
            f"daemon (pid {info.pid}) did not stop within {timeout_seconds:.0f}s; "
            f"refusing to {action} while it is running"
        )
    remove_pid_file(home)
    return True
