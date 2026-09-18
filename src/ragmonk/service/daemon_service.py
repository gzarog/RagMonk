"""Daemon start/stop/restart controls for the admin UI.

Admin UI plan, Phase 8 (§11.1): make the background daemon controllable
from the browser. Reuses the exact process-spawn, pid-file and health
primitives ``ragmonk daemon start|stop`` use (``cli.daemon._spawn`` and
``service.pid``) so a UI-started daemon is indistinguishable from a
CLI-started one -- either can stop the other.
"""

from __future__ import annotations

import time

from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import health, pid

_START_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.1


def start(ctx: AppContext) -> dict[str, str | int | bool | None]:
    from ragmonk.cli.daemon import _spawn  # noqa: PLC0415 - reuse the CLI's spawn logic

    existing = pid.running_daemon(ctx.home)
    if existing is not None:
        return {"running": True, "pid": existing.pid, "message": "already running"}

    child_pid = _spawn(ctx.home)
    pid.write_pid_file(ctx.home, child_pid)

    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    started = False
    while time.monotonic() < deadline:
        if not pid.is_process_alive(child_pid):
            break
        if health.read_health(ctx.home) is not None:
            started = True
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    if not started:
        pid.remove_pid_file(ctx.home)
        log_path = paths.logs_dir(ctx.home) / "daemon.out.log"
        raise UsageError(
            f"daemon did not report healthy within {_START_TIMEOUT_SECONDS:.0f}s; see {log_path}"
        )
    return {"running": True, "pid": child_pid, "message": "started"}


def stop(ctx: AppContext) -> dict[str, str | int | bool | None]:
    info = pid.running_daemon(ctx.home)
    if info is None:
        pid.remove_pid_file(ctx.home)
        return {"running": False, "pid": None, "message": "not running"}

    pid.signal_stop(info.pid)
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline and pid.is_process_alive(info.pid):
        time.sleep(_POLL_INTERVAL_SECONDS)

    if pid.is_process_alive(info.pid):
        raise UsageError(
            f"daemon (pid {info.pid}) did not stop within {_STOP_TIMEOUT_SECONDS:.0f}s"
        )
    pid.remove_pid_file(ctx.home)
    return {"running": False, "pid": info.pid, "message": "stopped"}


def restart(ctx: AppContext) -> dict[str, str | int | bool | None]:
    stop(ctx)
    return start(ctx)
