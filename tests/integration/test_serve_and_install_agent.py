"""``ragpilot serve --mcp`` (fast-fail paths only -- the success path
blocks on stdio forever and is deliberately not exercised here, see
``mcp/server.py``'s ``run_stdio`` docstring) and ``ragpilot install-agent``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.core.errors import EXIT_CONFIG_ERROR, EXIT_INVALID_ARGUMENTS


def test_serve_without_mcp_flag_is_invalid_arguments(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["serve"])

    assert result.exit_code == EXIT_INVALID_ARGUMENTS
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_serve_mcp_fails_fast_when_mcp_disabled(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    set_result = runner.invoke(app, ["config", "set", "mcp.enabled", "false"])
    assert set_result.exit_code == 0, set_result.output

    result = runner.invoke(app, ["serve", "--mcp"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "disabled" in result.output.lower()


def test_install_agent_prints_the_expected_mcp_client_snippet(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["install-agent"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "mcpServers": {
            "ragpilot": {
                "command": "ragpilot",
                "args": ["serve", "--mcp"],
            }
        }
    }


def test_install_agent_write_writes_exactly_that_content_and_nothing_else(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    sentinel = tmp_path / "untouched.json"
    sentinel.write_text('{"unrelated": true}', encoding="utf-8")

    target = tmp_path / "mcp-config" / "ragpilot.json"
    result = runner.invoke(app, ["install-agent", "--write", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == {
        "mcpServers": {
            "ragpilot": {
                "command": "ragpilot",
                "args": ["serve", "--mcp"],
            }
        }
    }
    printed = json.loads(result.stdout)
    assert printed == written
    # Rich soft-wraps long paths across lines in a narrow captured
    # console, so compare after collapsing hard line breaks.
    unwrapped_stderr = result.stderr.replace("\n", "")
    assert "Wrote" in unwrapped_stderr
    assert target.name in unwrapped_stderr

    # The command must never discover or edit any other file.
    assert sentinel.read_text(encoding="utf-8") == '{"unrelated": true}'


def _fake_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    # Path.home() reads $HOME on POSIX, %USERPROFILE% on Windows -- set
    # both so this is portable across the CI matrix without monkeypatching
    # Path.home() itself (which would affect any other code in the same
    # process that also calls it).
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_install_agent_client_claude_code_writes_mcp_json_with_type_field(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["install-agent", "--client", "claude-code"])

    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert written == {
        "mcpServers": {
            "ragpilot": {
                "type": "stdio",
                "command": "ragpilot",
                "args": ["serve", "--mcp"],
            }
        }
    }


def test_install_agent_client_vscode_uses_servers_key(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["install-agent", "--client", "vscode"])

    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    # VS Code's top-level key is "servers", not "mcpServers" -- the one
    # place this client's schema genuinely differs from the other three.
    assert written == {
        "servers": {
            "ragpilot": {
                "type": "stdio",
                "command": "ragpilot",
                "args": ["serve", "--mcp"],
            }
        }
    }


def test_install_agent_client_cursor_has_no_type_field(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["install-agent", "--client", "cursor"])

    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert written == {
        "mcpServers": {"ragpilot": {"command": "ragpilot", "args": ["serve", "--mcp"]}}
    }


def test_install_agent_client_json_merge_preserves_other_servers(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cursor_config = tmp_path / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    cursor_config.write_text(
        json.dumps({"mcpServers": {"other-tool": {"command": "other", "args": ["run"]}}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["install-agent", "--client", "cursor"])

    assert result.exit_code == 0, result.output
    written = json.loads(cursor_config.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other-tool"] == {"command": "other", "args": ["run"]}
    assert written["mcpServers"]["ragpilot"] == {"command": "ragpilot", "args": ["serve", "--mcp"]}


def test_install_agent_client_json_rerun_is_idempotent(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["install-agent", "--client", "claude-code"]).exit_code == 0

    result = runner.invoke(app, ["install-agent", "--client", "claude-code"])

    assert result.exit_code == 0, result.output
    assert "already configured" in result.output


def test_install_agent_client_codex_writes_toml(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _fake_home(monkeypatch, home)

    result = runner.invoke(app, ["install-agent", "--client", "codex"])

    assert result.exit_code == 0, result.output
    written = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[mcp_servers.ragpilot]' in written
    assert 'command = "ragpilot"' in written
    assert 'args = ["serve", "--mcp"]' in written


def test_install_agent_client_codex_merge_preserves_other_tables(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _fake_home(monkeypatch, home)
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.other_tool]\ncommand = "other"\nargs = ["run"]\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["install-agent", "--client", "codex"])

    assert result.exit_code == 0, result.output
    written = config_path.read_text(encoding="utf-8")
    assert 'command = "other"' in written
    assert "[mcp_servers.ragpilot]" in written


def test_install_agent_client_codex_never_corrupts_a_conflicting_entry(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _fake_home(monkeypatch, home)
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    original = '[mcp_servers.ragpilot]\ncommand = "custom-wrapper"\nargs = ["--special"]\n'
    config_path.write_text(original, encoding="utf-8")

    result = runner.invoke(app, ["install-agent", "--client", "codex"])

    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    # A duplicate [mcp_servers.ragpilot] table is invalid TOML -- appending
    # unconditionally would have corrupted the file. Nothing must change.
    assert config_path.read_text(encoding="utf-8") == original


def test_install_agent_client_all_configures_every_client(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _fake_home(monkeypatch, home)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["install-agent", "--client", "all"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".cursor" / "mcp.json").is_file()
    assert (tmp_path / ".vscode" / "mcp.json").is_file()
    assert (home / ".codex" / "config.toml").is_file()


def test_install_agent_client_and_write_are_mutually_exclusive(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["install-agent", "--client", "claude-code", "--write", str(tmp_path / "x.json")]
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_serve_help_documents_the_mcp_flag(ragpilot_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0

    # Not a substring check on the rendered --help text: Rich's console
    # width detection (and therefore where/whether it wraps or truncates
    # a long option row) varies by environment in ways collapsing "\n"
    # alone doesn't reliably fix -- observed passing locally under a
    # forced COLUMNS=80 but still failing on GitHub Actions' actual
    # (narrower, differently-detected) width. Ask Click directly instead,
    # which is exact and width-independent.
    serve_command = typer.main.get_command(app).get_command(None, "serve")  # type: ignore[union-attr]
    assert serve_command is not None
    assert any("--mcp" in opt.opts for opt in serve_command.params)
