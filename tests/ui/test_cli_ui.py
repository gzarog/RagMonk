"""``ragmonk ui`` CLI command options (§4.2/§17 CLI tests)."""

from __future__ import annotations

from typer.testing import CliRunner

from ragmonk.cli.main import app


def test_ui_help_lists_options(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output
    assert "--no-browser" in result.output


def test_ui_command_is_registered(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ui" in result.output


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
