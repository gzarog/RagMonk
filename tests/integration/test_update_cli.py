"""``ragpilot update [check|status|install]`` -- real Typer CLI dispatch,
with the one real network call (``checker.fetch_latest_release``, for
``check``/``status``) or the whole upgrade orchestration
(``installer.install_latest``, for ``install``) mocked out. Proves the
CLI wiring, cache read/write-through, JSON output, and exit codes -- not
the checker's own HTTP handling (``tests/unit/test_update_checker.py``)
or the installer's own orchestration
(``tests/unit/test_update_installer.py``), which cover those directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot import __version__
from ragpilot.cli.main import app
from ragpilot.core.errors import EXIT_GENERIC_FAILURE, EXIT_HEALTH_CHECK_FAILURE
from ragpilot.update import checker, installer
from ragpilot.update.installer import InstallOutcome, UpdateInstallError
from ragpilot.update.models import ReleaseInfo


def _fake_release(version: str) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag_name=f"v{version}",
        html_url=f"https://github.com/gzarog/Ragpilotv2/releases/tag/v{version}",
    )


def test_check_reports_update_available_and_writes_cache(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = "999.0.0"
    monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release(newer))

    result = runner.invoke(app, ["update", "check"])

    assert result.exit_code == 0, result.output
    assert f"Installed: {__version__}" in result.output
    assert f"Latest:    {newer}" in result.output
    assert "Update available." in result.output
    assert "ragpilot update install" in result.output

    status_result = runner.invoke(app, ["update", "status", "--json"])
    payload = json.loads(status_result.output)["data"]
    assert payload["latest_version"] == newer
    assert payload["status"] == "update available"


def test_check_reports_up_to_date(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release(__version__))

    result = runner.invoke(app, ["update", "check"])

    assert result.exit_code == 0, result.output
    assert "is up to date." in result.output
    assert "Update available." not in result.output


def test_check_json_output(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release("999.0.0"))

    result = runner.invoke(app, ["update", "check", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["installed_version"] == __version__
    assert payload["latest_version"] == "999.0.0"
    assert payload["update_available"] is True
    assert payload["release_url"].endswith("v999.0.0")


def test_check_surfaces_a_clear_error_when_github_is_unreachable(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(**_: object) -> ReleaseInfo:
        raise checker.UpdateCheckError("GitHub is unreachable: simulated")

    monkeypatch.setattr(checker, "fetch_latest_release", _raise)

    result = runner.invoke(app, ["update", "check"])

    assert result.exit_code == EXIT_GENERIC_FAILURE
    assert "could not check for updates" in result.output
    assert "GitHub is unreachable" in result.output


def test_bare_update_behaves_like_check(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release("999.0.0"))

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert "Update available." in result.output


def test_status_with_no_prior_check_reports_unknown(
    ragpilot_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["update", "status"])

    assert result.exit_code == 0, result.output
    assert f"Installed: {__version__}" in result.output
    assert "unknown" in result.output


def test_status_json_with_no_prior_check(ragpilot_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["update", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["latest_version"] is None
    assert payload["status"] == "unknown"


def test_install_already_up_to_date(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer,
        "install_latest",
        lambda home, **_: InstallOutcome(
            installed_version=__version__, upgraded=False, migrations_applied=False, healthy=True
        ),
    )

    result = runner.invoke(app, ["update", "install"])

    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_install_success_reports_each_step(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer,
        "install_latest",
        lambda home, **_: InstallOutcome(
            installed_version="999.0.0", upgraded=True, migrations_applied=True, healthy=True
        ),
    )

    result = runner.invoke(app, ["update", "install"])

    assert result.exit_code == 0, result.output
    assert "Installed RAGpilot 999.0.0" in result.output
    assert "Database migrations complete" in result.output
    assert "Health check passed" in result.output
    assert "RAGpilot 999.0.0 is ready." in result.output


def test_install_json_output(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer,
        "install_latest",
        lambda home, **_: InstallOutcome(
            installed_version="999.0.0", upgraded=True, migrations_applied=True, healthy=True
        ),
    )

    result = runner.invoke(app, ["update", "install", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload == {
        "installed_version": "999.0.0",
        "upgraded": True,
        "migrations_applied": True,
        "healthy": True,
    }


def test_install_unhealthy_after_upgrade_exits_with_health_check_failure(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer,
        "install_latest",
        lambda home, **_: InstallOutcome(
            installed_version="999.0.0", upgraded=True, migrations_applied=True, healthy=False
        ),
    )

    result = runner.invoke(app, ["update", "install"])

    assert result.exit_code == EXIT_HEALTH_CHECK_FAILURE
    assert "Health check reported issues" in result.output


def test_install_surfaces_a_clear_error_for_an_unsupported_install_method(
    ragpilot_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(home: Path, **_: object) -> None:
        raise UpdateInstallError("this is an editable/dev RAGpilot install; use `git pull`")

    monkeypatch.setattr(installer, "install_latest", _raise)

    result = runner.invoke(app, ["update", "install"])

    assert result.exit_code == EXIT_GENERIC_FAILURE
    assert "git pull" in result.output
