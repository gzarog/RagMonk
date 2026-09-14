"""``ragpilot daemon start|stop|restart|status``.

``start`` spawns a detached background process running the exact same
daemon loop ``ragpilot watch`` runs in the foreground (``cli/watch.py``,
which in turn drives ``service/daemon.Daemon``); ``stop`` signals that
process to shut down gracefully; ``status`` reports on it by reading its
PID file (``service/pid.py``) and heartbeat snapshot
(``service/health.py``) from this, separate, process -- there is no
local RPC/socket in this phase, so a fresh CLI invocation can only ever
read what the running daemon last wrote, not ask it live.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from ragpilot.core import paths
from ragpilot.core.errors import UsageError
from ragpilot.core.lifecycle import AppContext
from ragpilot.service import health, pid

from ._common import cli_command, console, print_json

app = typer.Typer(no_args_is_help=True, help="Manage the RAGpilot background daemon.")

# A background daemon process needs a moment to import, bootstrap
# AppContext, apply migrations and subscribe its first watchers before
# its health snapshot exists -- generous but bounded so a genuinely
# broken start (bad config, crash-on-import) fails `daemon start` instead
# of hanging. The import chain alone (docling -> transformers -> torch)
# can take several seconds on a slow/loaded machine (observed timing out
# at 10s on Windows CI, where process startup and cold imports are
# consistently slower than Linux/macOS in this project's own CI history)
# well before the daemon does any real work.
_START_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.1


def _spawn(home: Path) -> int:
    log_path = paths.logs_dir(home) / "daemon.out.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # No process-group/session concept to detach with on Windows --
        # CREATE_NEW_PROCESS_GROUP is the equivalent for the "a Ctrl+C to
        # this CLI's console doesn't reach the child" half.
        #
        # For "no console window", DETACHED_PROCESS alone is not enough:
        # Microsoft's own docs describe it as "no console handle set",
        # but in practice CreateProcess can still allocate a new console
        # for a console-subsystem child (python.exe is one) under
        # DETACHED_PROCESS, producing exactly the visible extra window
        # reported live -- `ragpilot daemon start` popping open a second
        # terminal window instead of returning silently to the caller's
        # own console. CREATE_NO_WINDOW is the flag whose specific job is
        # suppressing window creation for a console-subsystem process;
        # combined with the two above, the child is both detached and
        # genuinely invisible.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        # Detaches the child into its own session so it survives this
        # CLI process exiting and isn't killed by a signal sent to this
        # process's process group (e.g. an interactive shell's Ctrl+C).
        kwargs["start_new_session"] = True
    process = subprocess.Popen(  # noqa: SIM115 - handle deliberately outlives this call
        [sys.executable, "-m", "ragpilot.cli.main", "watch"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        **kwargs,
    )
    return process.pid


@app.command("start")
@cli_command
def start() -> None:
    with AppContext.bootstrap() as ctx:
        existing = pid.running_daemon(ctx.home)
        if existing is not None:
            console.print(f"[yellow]Daemon already running[/yellow] (pid {existing.pid})")
            return

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
                f"daemon did not report healthy within {_START_TIMEOUT_SECONDS:.0f}s; "
                f"see {log_path}"
            )
        console.print(f"[green]Daemon started[/green] (pid {child_pid})")


@app.command("stop")
@cli_command
def stop() -> None:
    with AppContext.bootstrap() as ctx:
        info = pid.running_daemon(ctx.home)
        if info is None:
            pid.remove_pid_file(ctx.home)
            console.print("[yellow]Daemon is not running[/yellow]")
            return

        pid.signal_stop(info.pid)
        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline and pid.is_process_alive(info.pid):
            time.sleep(_POLL_INTERVAL_SECONDS)

        if pid.is_process_alive(info.pid):
            raise UsageError(
                f"daemon (pid {info.pid}) did not stop within {_STOP_TIMEOUT_SECONDS:.0f}s"
            )

        pid.remove_pid_file(ctx.home)
        console.print(f"[green]Daemon stopped[/green] (pid {info.pid})")


@app.command("restart")
@cli_command
def restart() -> None:
    stop()
    start()


def _uptime_seconds(started_at: str) -> float:
    started = datetime.fromisoformat(started_at)
    return (datetime.now(UTC) - started).total_seconds()


@app.command("status")
@cli_command
def status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with AppContext.bootstrap() as ctx:
        info = pid.running_daemon(ctx.home)
        snapshot = health.read_health(ctx.home)

        data: dict[str, Any] = {
            "running": info is not None,
            "pid": info.pid if info else None,
            "started_at": info.started_at if info else None,
            "uptime_seconds": _uptime_seconds(info.started_at) if info else None,
            "last_reconciliation_at": snapshot.last_reconciliation_at if snapshot else None,
            "sources": [
                {
                    "source_id": s.source_id,
                    "path": s.path,
                    "source_type": s.source_type,
                    "online": s.online,
                    "last_pass_at": s.last_pass_at,
                }
                for s in (snapshot.sources if snapshot else [])
            ],
        }

        if json_output:
            print_json(data)
            return

        if info is not None:
            console.print(
                f"[green]RUNNING[/green] pid={info.pid} "
                f"uptime={data['uptime_seconds']:.0f}s"
            )
        else:
            console.print("[yellow]STOPPED[/yellow]")
        console.print(f"Last reconciliation: {data['last_reconciliation_at'] or '-'}")
        for source in data["sources"]:
            state = "online" if source["online"] else "[red]OFFLINE[/red]"
            console.print(
                f"  {source['source_id']} ({source['source_type']}) {state} "
                f"last_pass={source['last_pass_at'] or '-'}"
            )
