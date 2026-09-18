"""``ragmonk daemon status|stop`` fast paths (no daemon running -- these
never spawn a real process) and the ``daemon``/``watch`` commands' CLI
wiring. Real process start/stop is exercised separately, behind the
``daemon_subprocess`` marker -- see ``test_daemon_subprocess.py`` and
CONTRIBUTING.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from ragmonk.cli.main import app


def test_daemon_status_when_never_started(ragmonk_home: Path, runner: CliRunner) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["daemon", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["data"]
    assert payload["running"] is False
    assert payload["pid"] is None
    assert payload["sources"] == []


def test_daemon_status_human_output_when_stopped(ragmonk_home: Path, runner: CliRunner) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.output
    assert "STOPPED" in result.output


def test_daemon_stop_when_not_running_is_a_no_op_success(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert "not running" in result.output.lower()


def test_daemon_help_lists_subcommands(ragmonk_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    for name in ("start", "stop", "restart", "status"):
        assert name in result.output


def test_watch_is_wired_into_main(ragmonk_home: Path, runner: CliRunner) -> None:
    watch_command = typer.main.get_command(app).get_command(None, "watch")  # type: ignore[union-attr]
    assert watch_command is not None
