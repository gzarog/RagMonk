from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core import paths


def test_runtime_dir_respects_ragmonk_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGMONK_HOME", str(tmp_path / "custom"))
    assert paths.runtime_dir() == tmp_path / "custom"


def test_ensure_runtime_layout_creates_expected_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_runtime_layout(home)
    assert (home / "projects").is_dir()
    assert (home / "logs").is_dir()
    assert (home / "backups").is_dir()
    assert (home / "locks").is_dir()
    assert (home / "tmp").is_dir()


def test_project_id_is_stable_for_same_path(tmp_path: Path) -> None:
    target = tmp_path / "source"
    target.mkdir()
    assert paths.project_id_for_path(target) == paths.project_id_for_path(target)


def test_project_id_differs_for_different_paths(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert paths.project_id_for_path(a) != paths.project_id_for_path(b)


def test_windows_runtime_dir_uses_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises the win32 branch by faking sys.platform; the actual OS is
    still Linux, so this only proves the path-construction logic, not real
    filesystem behavior under Windows.
    """
    monkeypatch.delenv("RAGMONK_HOME", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert paths.runtime_dir() == tmp_path / "AppData" / "Local" / "RagMonk"
