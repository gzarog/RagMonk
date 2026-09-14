"""``update/installer.py``: install-method detection (metadata file first,
then runtime heuristics) and ``install_latest``'s orchestration --
check/validate/detect/upgrade/migrate/health-check -- with the actual
subprocess calls replaced by a recording fake ``CommandRunner`` so no
real process (curl, pip, pipx, powershell) is ever spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragpilot import __version__
from ragpilot.update import checker, installer
from ragpilot.update.installer import InstallMethod, UpdateInstallError
from ragpilot.update.models import ReleaseInfo

_NEWER_TAG = "v999.0.0"
_NEWER_VERSION = "999.0.0"


def _fake_release(version: str = _NEWER_VERSION, tag: str = _NEWER_TAG) -> ReleaseInfo:
    return ReleaseInfo(version=version, tag_name=tag, html_url=f"https://x/{tag}")


class _RecordingRunner:
    def __init__(self, *, exit_codes: list[int] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self._exit_codes = list(exit_codes) if exit_codes is not None else None

    def __call__(self, cmd: list[str], env: dict[str, str] | None = None) -> int:
        self.calls.append((list(cmd), env))
        if self._exit_codes is not None:
            return self._exit_codes.pop(0)
        return 0


class TestDetectInstallMethod:
    def test_metadata_file_is_authoritative(self, tmp_path: Path) -> None:
        import json

        home = tmp_path
        home.mkdir(parents=True, exist_ok=True)
        (home / "install_info.json").write_text(
            json.dumps({"install_method": "install-script", "repository": "gzarog/Ragpilotv2"}),
            encoding="utf-8",
        )
        assert installer.detect_install_method(home) is InstallMethod.INSTALL_SCRIPT

    def test_no_metadata_and_no_distribution_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.metadata

        def _raise(name: str) -> None:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", _raise)
        assert installer.detect_install_method(tmp_path) is InstallMethod.UNKNOWN

    def test_editable_install_detected_via_direct_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "_is_editable_install", lambda: True)
        assert installer.detect_install_method(tmp_path) is InstallMethod.EDITABLE

    def test_pipx_shaped_prefix_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "_is_editable_install", lambda: False)
        monkeypatch.setattr(installer, "_is_pipx_install", lambda: True)
        assert installer.detect_install_method(tmp_path) is InstallMethod.PIPX

    def test_falls_back_to_pip_when_a_distribution_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.metadata

        monkeypatch.setattr(installer, "_is_editable_install", lambda: False)
        monkeypatch.setattr(installer, "_is_pipx_install", lambda: False)
        monkeypatch.setattr(importlib.metadata, "distribution", lambda name: object())
        assert installer.detect_install_method(tmp_path) is InstallMethod.PIP


class TestInstallLatest:
    def test_already_up_to_date_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            checker, "fetch_latest_release", lambda **_: _fake_release(version=__version__)
        )
        runner = _RecordingRunner()

        outcome = installer.install_latest(tmp_path, runner=runner)

        assert outcome.upgraded is False
        assert outcome.installed_version == __version__
        assert runner.calls == []

    def test_check_failure_raises_update_install_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(**_: object) -> None:
            raise checker.UpdateCheckError("offline")

        monkeypatch.setattr(checker, "fetch_latest_release", _raise)

        with pytest.raises(UpdateInstallError, match="could not check for updates"):
            installer.install_latest(tmp_path, runner=_RecordingRunner())

    def test_editable_install_refuses_with_git_pull_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.EDITABLE)

        with pytest.raises(UpdateInstallError, match="git pull"):
            installer.install_latest(tmp_path, runner=_RecordingRunner())

    def test_unknown_install_refuses_with_manual_instructions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.UNKNOWN)

        with pytest.raises(UpdateInstallError, match="install.sh"):
            installer.install_latest(tmp_path, runner=_RecordingRunner())

    def test_pip_install_runs_pip_upgrade_then_migrate_then_health_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.PIP)
        runner = _RecordingRunner()

        outcome = installer.install_latest(tmp_path, runner=runner)

        assert outcome.upgraded is True
        assert outcome.installed_version == _NEWER_VERSION
        assert outcome.migrations_applied is True
        assert outcome.healthy is True
        assert len(runner.calls) == 3
        pip_argv, pip_env = runner.calls[0]
        assert pip_argv[1:3] == ["-m", "pip"]
        assert pip_argv[-1].endswith(f"@{_NEWER_TAG}")
        assert pip_env is None
        assert runner.calls[1][0][-1] == "upgrade"
        assert runner.calls[2][0][-1] == "doctor"

    def test_install_script_method_passes_ref_via_env_not_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(
            installer, "detect_install_method", lambda home: InstallMethod.INSTALL_SCRIPT
        )
        runner = _RecordingRunner()

        installer.install_latest(tmp_path, runner=runner)

        argv, env = runner.calls[0]
        assert _NEWER_TAG not in " ".join(argv)
        assert env == {"RAGPILOT_REF": _NEWER_TAG}

    def test_pipx_install_runs_pipx_install_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.PIPX)
        runner = _RecordingRunner()

        installer.install_latest(tmp_path, runner=runner)

        argv, _env = runner.calls[0]
        assert argv[:3] == ["pipx", "install", "--force"]
        assert argv[-1].endswith(f"@{_NEWER_TAG}")

    def test_upgrade_command_failure_raises_before_migrating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.PIP)
        runner = _RecordingRunner(exit_codes=[1])

        with pytest.raises(UpdateInstallError, match="exited with code 1"):
            installer.install_latest(tmp_path, runner=runner)

        assert len(runner.calls) == 1  # never reached the migrate/health-check steps

    def test_migration_or_health_check_failure_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checker, "fetch_latest_release", lambda **_: _fake_release())
        monkeypatch.setattr(installer, "detect_install_method", lambda home: InstallMethod.PIP)
        runner = _RecordingRunner(exit_codes=[0, 1, 0])  # upgrade ok, migration fails, health ok

        outcome = installer.install_latest(tmp_path, runner=runner)

        assert outcome.upgraded is True
        assert outcome.migrations_applied is False
        assert outcome.healthy is True
