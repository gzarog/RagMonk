"""``ops/uninstall.py``: install-method-aware uninstall planning, the
per-method application-removal dispatch (an injectable runner records
calls -- no real ``pip``/``pipx`` process is ever spawned), and
``purge_home``'s daemon-stop-then-delete sequence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragpilot.core import paths
from ragpilot.ops import uninstall as uninstall_ops
from ragpilot.service import pid
from ragpilot.update.installer import InstallMethod


class _RecordingRunner:
    def __init__(self, *, exit_codes: list[int] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self._exit_codes = list(exit_codes) if exit_codes is not None else None

    def __call__(self, cmd: list[str], env: dict[str, str] | None = None) -> int:
        self.calls.append((list(cmd), env))
        if self._exit_codes is not None:
            return self._exit_codes.pop(0)
        return 0


def _write_install_info(home: Path, **fields: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    paths.install_info_path(home).write_text(json.dumps(fields), encoding="utf-8")


class TestPlanUninstall:
    def test_no_metadata_and_no_distribution_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.metadata

        def _raise(name: str) -> None:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", _raise)

        plan = uninstall_ops.plan_uninstall(tmp_path)

        assert plan.method is InstallMethod.UNKNOWN
        assert plan.can_auto_remove_app is False
        assert plan.manual_instructions is not None

    def test_install_script_with_full_metadata_lists_exact_paths(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _write_install_info(
            home,
            install_method="install-script",
            repository="gzarog/Ragpilotv2",
            install_dir="/opt/ragpilot",
            venv_dir="/opt/ragpilot/venv",
            bin_dir="/opt/bin",
        )

        plan = uninstall_ops.plan_uninstall(home)

        assert plan.method is InstallMethod.INSTALL_SCRIPT
        assert plan.can_auto_remove_app is True
        assert Path("/opt/ragpilot/venv") in plan.app_paths
        assert Path("/opt/ragpilot/app") in plan.app_paths
        assert any(p.parent == Path("/opt/bin") for p in plan.app_paths)

    def test_install_script_missing_paths_refuses_rather_than_guess(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _write_install_info(
            home, install_method="install-script", repository="gzarog/Ragpilotv2"
        )

        plan = uninstall_ops.plan_uninstall(home)

        assert plan.can_auto_remove_app is False
        assert plan.app_paths == []

    def test_pip_and_pipx_are_auto_removable_with_no_listed_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ragpilot.update import installer

        monkeypatch.setattr(installer, "_is_editable_install", lambda: False)
        monkeypatch.setattr(installer, "_is_pipx_install", lambda: False)
        monkeypatch.setattr(
            "importlib.metadata.distribution", lambda name: object()
        )

        plan = uninstall_ops.plan_uninstall(tmp_path)

        assert plan.method is InstallMethod.PIP
        assert plan.can_auto_remove_app is True
        assert plan.app_paths == []

    def test_editable_install_refuses_with_git_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ragpilot.update import installer

        monkeypatch.setattr(installer, "_is_editable_install", lambda: True)

        plan = uninstall_ops.plan_uninstall(tmp_path)

        assert plan.method is InstallMethod.EDITABLE
        assert plan.can_auto_remove_app is False
        assert "source checkout" in (plan.manual_instructions or "")


class TestRemoveApplication:
    def test_not_auto_removable_returns_false_without_running_anything(
        self, tmp_path: Path
    ) -> None:
        plan = uninstall_ops.UninstallPlan(method=InstallMethod.UNKNOWN, can_auto_remove_app=False)
        runner = _RecordingRunner()

        assert uninstall_ops.remove_application(plan, runner=runner) is False
        assert runner.calls == []

    def test_pip_runs_pip_uninstall(self, tmp_path: Path) -> None:
        plan = uninstall_ops.UninstallPlan(method=InstallMethod.PIP, can_auto_remove_app=True)
        runner = _RecordingRunner()

        assert uninstall_ops.remove_application(plan, runner=runner) is True
        argv, env = runner.calls[0]
        assert argv[1:] == ["-m", "pip", "uninstall", "-y", "ragpilot"]
        assert env is None

    def test_pipx_runs_pipx_uninstall(self, tmp_path: Path) -> None:
        plan = uninstall_ops.UninstallPlan(method=InstallMethod.PIPX, can_auto_remove_app=True)
        runner = _RecordingRunner()

        assert uninstall_ops.remove_application(plan, runner=runner) is True
        assert runner.calls[0][0] == ["pipx", "uninstall", "ragpilot"]

    def test_pip_failure_is_reported_as_false(self, tmp_path: Path) -> None:
        plan = uninstall_ops.UninstallPlan(method=InstallMethod.PIP, can_auto_remove_app=True)
        runner = _RecordingRunner(exit_codes=[1])

        assert uninstall_ops.remove_application(plan, runner=runner) is False

    def test_install_script_removes_exact_paths_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Forces the direct, synchronous (POSIX) removal path regardless
        # of the OS actually running this test: the Windows path defers
        # deletion to a detached, delayed process (see
        # test_windows_spawns_a_detached_delayed_delete below), which
        # this test isn't set up to wait for -- what's under test here is
        # *which* paths get removed, not *how*.
        monkeypatch.setattr(uninstall_ops.sys, "platform", "linux")

        venv_dir = tmp_path / "install" / "venv"
        app_dir = tmp_path / "install" / "app"
        bin_dir = tmp_path / "bin"
        other_file = bin_dir / "some-other-tool"
        launcher = bin_dir / "ragpilot"
        for d in (venv_dir, app_dir, bin_dir):
            d.mkdir(parents=True)
        (venv_dir / "marker").write_text("x")
        (app_dir / "marker").write_text("x")
        other_file.write_text("keep me")
        launcher.write_text("launcher")

        plan = uninstall_ops.UninstallPlan(
            method=InstallMethod.INSTALL_SCRIPT,
            can_auto_remove_app=True,
            app_paths=[venv_dir, app_dir, launcher],
        )

        result = uninstall_ops.remove_application(plan)

        assert result is True
        assert not venv_dir.exists()
        assert not app_dir.exists()
        assert not launcher.exists()
        assert other_file.is_file()  # sibling file in the shared bin dir survives
        assert other_file.read_text() == "keep me"

    def test_windows_spawns_a_detached_delayed_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(uninstall_ops.sys, "platform", "win32")
        calls: list[tuple[list[str], dict[str, object]]] = []
        monkeypatch.setattr(
            uninstall_ops.subprocess,
            "Popen",
            lambda cmd, **kw: calls.append((cmd, kw)),
        )

        venv_dir = tmp_path / "install" / "venv"
        app_dir = tmp_path / "install" / "app"
        launcher = tmp_path / "bin" / "ragpilot.cmd"
        plan = uninstall_ops.UninstallPlan(
            method=InstallMethod.INSTALL_SCRIPT,
            can_auto_remove_app=True,
            app_paths=[venv_dir, app_dir, launcher],
        )

        result = uninstall_ops.remove_application(plan)

        assert result is True
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        script = cmd[-1]
        assert str(venv_dir) in script
        assert str(app_dir) in script
        assert str(launcher) in script
        assert "creationflags" in kwargs


class TestPurgeHome:
    def test_missing_home_returns_false(self, tmp_path: Path) -> None:
        assert uninstall_ops.purge_home(tmp_path / "does-not-exist") is False

    def test_deletes_an_existing_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "sources.db").write_text("data")

        assert uninstall_ops.purge_home(home) is True
        assert not home.exists()

    def test_stops_a_running_daemon_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        calls: list[str] = []
        monkeypatch.setattr(
            pid, "stop_and_wait", lambda h, **kw: calls.append(str(h)) or True
        )

        uninstall_ops.purge_home(home)

        assert calls == [str(home)]
