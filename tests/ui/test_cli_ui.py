"""``ragmonk ui`` CLI command options (§4.2/§17 CLI tests)."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from ragmonk.cli.main import app

# Typer renders --help through Rich when it is installed, which styles the
# text with ANSI escapes and, crucially, *soft-wraps and truncates* the
# options table to the terminal width (80 columns for a non-tty like CI's
# pytest). At 80 columns a flag such as ``--host`` can be split across a
# wrap or truncated with an ellipsis, so a naive ``"--host" in output``
# substring check is flaky across Rich/Typer versions and widths (it
# passes locally, fails on CI). Rendering at a wide width and stripping
# ANSI makes the assertion depend on the command's real wiring, not on how
# Rich happened to lay the help out.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_WIDE_ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}


def _help_text(runner: CliRunner, args: list[str]) -> str:
    result = runner.invoke(app, args, env=_WIDE_ENV)
    assert result.exit_code == 0, result.output
    # Rich also inserts hard line breaks to fit the (now wide) width; drop
    # newlines too so a flag never straddles a wrap boundary.
    return _ANSI.sub("", result.output).replace("\n", " ")


def test_ui_help_lists_options(runner: CliRunner) -> None:
    text = _help_text(runner, ["ui", "--help"])
    assert "--host" in text
    assert "--port" in text
    assert "--no-browser" in text


def test_ui_command_is_registered(runner: CliRunner) -> None:
    text = _help_text(runner, ["--help"])
    assert "ui" in text


def test_ui_starts_and_serves(monkeypatch, ragmonk_home, tmp_path) -> None:
    """`ragmonk ui --no-browser` builds the app and starts uvicorn.

    uvicorn.run is stubbed so the test never actually blocks on a server;
    we assert the command wired the app + host/port through correctly.
    """
    monkeypatch.setenv("RAGMONK_HOME", str(ragmonk_home))
    captured = {}

    def fake_run(app_obj, **kwargs):  # noqa: ANN001, ANN003
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")
        captured["app"] = app_obj

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["ui", "--no-browser", "--port", "9999"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["app"] is not None
