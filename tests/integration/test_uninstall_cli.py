"""``ragpilot uninstall`` -- real Typer CLI dispatch against a real,
``ragpilot_home``-isolated directory. This whole test suite always runs
against an editable install (``pip install -e ".[dev]"``, both locally
and in CI -- see CONTRIBUTING.md), so ``detect_install_method`` reliably
reports ``"editable"`` here; the per-method dispatch itself is covered
directly, with a recording runner, in ``tests/unit/test_ops_uninstall.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.ops import uninstall as uninstall_ops


def test_declining_confirmation_removes_nothing(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert ragpilot_home.is_dir()

    result = runner.invoke(app, ["uninstall"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert ragpilot_home.is_dir()
    assert (ragpilot_home / "sources.db").is_file()


def test_yes_purges_data_and_reports_manual_app_removal(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert not ragpilot_home.exists()
    assert "source checkout" in result.output  # editable-install manual instructions


def test_keep_data_leaves_home_untouched(ragpilot_home: Path, runner: CliRunner) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    result = runner.invoke(app, ["uninstall", "--keep-data", "--yes"])

    assert result.exit_code == 0, result.output
    assert ragpilot_home.is_dir()
    assert (ragpilot_home / "sources.db").is_file()


def test_json_output_reports_the_plan_and_outcome(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    result = runner.invoke(app, ["uninstall", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["install_method"] == "editable"
    assert payload["data_purged"] is True
    assert payload["app_removed"] is False
    assert payload["manual_instructions"] is not None


def test_uninstall_with_no_prior_home_reports_nothing_to_purge(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    assert not ragpilot_home.exists()

    result = runner.invoke(app, ["uninstall", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["data_purged"] is False


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_both_yes_spellings_skip_the_prompt(
    ragpilot_home: Path, runner: CliRunner, flag: str
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    result = runner.invoke(app, ["uninstall", flag])

    assert result.exit_code == 0, result.output
    assert "Proceed?" not in result.output


def test_app_removal_runs_before_the_data_purge(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # install.sh/install.ps1's default layout nests the venv *inside*
    # RAGPILOT_HOME -- purging data before removing the application would
    # delete the very interpreter a pip/pipx uninstall needs (see
    # ops/uninstall.py's remove_application docstring). Order, not
    # outcome, is what this test guards.
    calls: list[str] = []
    monkeypatch.setattr(
        uninstall_ops, "remove_application", lambda plan: calls.append("remove_application") or True
    )
    monkeypatch.setattr(
        uninstall_ops, "purge_home", lambda home: calls.append("purge_home") or True
    )
    assert runner.invoke(app, ["init"]).exit_code == 0

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == ["remove_application", "purge_home"]
