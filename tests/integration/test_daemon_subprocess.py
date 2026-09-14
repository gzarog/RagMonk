"""``ragpilot daemon start|stop|restart`` against a real, detached OS
process. Marked ``daemon_subprocess`` and excluded from the default run
(see ``pyproject.toml``) -- spawning and signaling a real process is
slower and more platform-fragile than anything else in the suite, the
same tradeoff Phase 3 already made for ``docling_pdf``. The daemon
*loop* itself (watcher wiring, debounce, reconciliation, offline/online
transitions, graceful shutdown) is fully covered by
``test_daemon.py``/``test_service_pid.py``/``test_service_health.py`` in
the default suite -- this file only proves the process-spawning and
signal-delivery plumbing around it actually works.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.core import paths

pytestmark = pytest.mark.daemon_subprocess


def _pid_owns_a_visible_window(  # pragma: no cover - exercised only on Windows
    pid: int, *, timeout_seconds: float = 3.0
) -> bool:
    """True if any top-level window on the desktop belongs to ``pid`` --
    the real-world, live-reported symptom this guards against (`ragpilot
    daemon start` popping open a second, visible console window instead
    of returning silently) has no CI-visible trace beyond an actual
    window existing on the runner's desktop, so this checks the Win32
    window list directly (``ctypes``, same direct-WinAPI-access pattern
    as ``service/pid.py``) rather than trusting the spawn flags alone.
    Polls briefly: a console host can take a moment to materialize after
    CreateProcess returns, and this needs to catch one if it's there,
    not just check the instant after spawn.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = False

    def _callback(hwnd: int, _lparam: int) -> bool:
        nonlocal found
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid and user32.IsWindowVisible(hwnd):
            found = True
            return False  # stop enumeration
        return True

    enum_proc = WNDENUMPROC(_callback)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not found:
        user32.EnumWindows(enum_proc, 0)
        if found:
            break
        time.sleep(0.2)
    return found


def test_daemon_start_stop_full_cycle(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0

    daemon_log = paths.logs_dir(ragpilot_home) / "daemon.out.log"
    start = runner.invoke(app, ["daemon", "start"])
    assert start.exit_code == 0, (start.output, daemon_log.read_text())

    try:
        status = runner.invoke(app, ["daemon", "status", "--json"])
        assert status.exit_code == 0, status.output
        payload = json.loads(status.stdout)["data"]
        assert payload["running"] is True
        assert isinstance(payload["pid"], int)

        # The spawned daemon indexes its one source on startup -- poll
        # until that shows up in the health snapshot it wrote.
        deadline = time.monotonic() + 10
        indexed_a_source = False
        while time.monotonic() < deadline:
            status = runner.invoke(app, ["daemon", "status", "--json"])
            payload = json.loads(status.stdout)["data"]
            if payload["sources"] and payload["sources"][0]["last_pass_at"]:
                indexed_a_source = True
                break
            time.sleep(0.2)
        assert indexed_a_source, payload

        second_start = runner.invoke(app, ["daemon", "start"])
        assert second_start.exit_code == 0
        assert "already running" in second_start.output.lower()
    finally:
        stop = runner.invoke(app, ["daemon", "stop"])
        assert stop.exit_code == 0, stop.output

    status = runner.invoke(app, ["daemon", "status", "--json"])
    payload = json.loads(status.stdout)["data"]
    assert payload["running"] is False


def test_daemon_restart_stops_and_starts_a_fresh_process(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0

    assert runner.invoke(app, ["daemon", "start"]).exit_code == 0
    first_status = runner.invoke(app, ["daemon", "status", "--json"])
    first_pid = json.loads(first_status.stdout)["data"]["pid"]

    try:
        restart = runner.invoke(app, ["daemon", "restart"])
        assert restart.exit_code == 0, restart.output

        second_status = runner.invoke(app, ["daemon", "status", "--json"])
        payload = json.loads(second_status.stdout)["data"]
        assert payload["running"] is True
        assert payload["pid"] != first_pid
    finally:
        runner.invoke(app, ["daemon", "stop"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only spawn-flag regression")
def test_daemon_start_on_windows_opens_no_visible_window(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    # Reported live: `ragpilot daemon start` popped open a second, visible
    # console window instead of returning silently to the caller's own
    # terminal -- DETACHED_PROCESS alone doesn't reliably suppress a
    # console window for a console-subsystem child (python.exe) on
    # Windows; see cli/daemon.py's _spawn for the CREATE_NO_WINDOW fix
    # this guards.
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0

    start = runner.invoke(app, ["daemon", "start"])
    assert start.exit_code == 0, start.output

    try:
        status = runner.invoke(app, ["daemon", "status", "--json"])
        pid = json.loads(status.stdout)["data"]["pid"]
        assert isinstance(pid, int)

        assert not _pid_owns_a_visible_window(pid), (
            f"daemon process {pid} owns a visible window -- "
            "DETACHED_PROCESS/CREATE_NO_WINDOW isn't suppressing it"
        )
    finally:
        runner.invoke(app, ["daemon", "stop"])
