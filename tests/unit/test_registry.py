from __future__ import annotations

import os
from pathlib import Path

import pytest

from ragmonk.core.errors import RagMonkError, SourceUnavailableError, UsageError
from ragmonk.core.models import SourceType
from ragmonk.service import pid
from ragmonk.sources import registry as registry_module
from ragmonk.sources.registry import SourceRegistry, detect_source_type, make_source_id
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import sources_repo
from ragmonk.storage.sqlite import connect


@pytest.mark.parametrize(
    "raw_path,expected",
    [
        ("/home/user/project", SourceType.LOCAL),
        ("C:\\Users\\me\\project", SourceType.LOCAL),
        ("//fileserver/share", SourceType.NETWORK),
        ("\\\\fileserver\\share", SourceType.NETWORK),
        ("smb://fileserver/share", SourceType.NETWORK),
    ],
)
def test_detect_source_type(raw_path: str, expected: SourceType) -> None:
    assert detect_source_type(raw_path) is expected


def test_make_source_id_is_stable_and_prefixed() -> None:
    assert make_source_id("/a/b") == make_source_id("/a/b")
    assert make_source_id("/a/b").startswith("src_")


def test_add_rejects_missing_path(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sources.db")
    apply_migrations(conn, "sources")
    registry = SourceRegistry(conn, home=tmp_path / "home")
    with pytest.raises(SourceUnavailableError):
        registry.add(str(tmp_path / "missing"))


def test_add_is_idempotent_for_same_path(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sources.db")
    apply_migrations(conn, "sources")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    registry = SourceRegistry(conn, home=tmp_path / "home")
    first = registry.add(str(source_dir))
    second = registry.add(str(source_dir))
    assert first.id == second.id


def test_get_unknown_source_is_usage_error(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sources.db")
    apply_migrations(conn, "sources")
    registry = SourceRegistry(conn, home=tmp_path / "home")
    with pytest.raises(UsageError):
        registry.get("does-not-exist")


def _registry(tmp_path: Path) -> SourceRegistry:
    conn = connect(tmp_path / "sources.db")
    apply_migrations(conn, "sources")
    return SourceRegistry(conn, home=tmp_path / "home")


def test_remove_unknown_source_is_usage_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(UsageError):
        registry.remove("does-not-exist")


def test_remove_deletes_project_dir_and_registry_row(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    registry = _registry(tmp_path)
    source = registry.add(str(source_dir))
    project_dir = registry.project_dir_for(source)
    assert project_dir.is_dir()

    outcome = registry.remove(source.id)

    assert outcome.project_dir_deleted is True
    assert outcome.project_dir == project_dir
    assert not project_dir.exists()
    assert sources_repo.get(registry._conn, source.id) is None  # noqa: SLF001 - test-only introspection
    # The original source directory itself is never touched.
    assert source_dir.is_dir()


def test_remove_without_a_project_dir_still_removes_the_row(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    registry = _registry(tmp_path)
    source = registry.add(str(source_dir))
    project_dir = registry.project_dir_for(source)
    import shutil

    shutil.rmtree(project_dir)
    assert not project_dir.exists()

    outcome = registry.remove(source.id)

    assert outcome.project_dir_deleted is False
    assert sources_repo.get(registry._conn, source.id) is None  # noqa: SLF001 - test-only introspection


def test_remove_keeps_the_source_registered_when_deletion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    registry = _registry(tmp_path)
    source = registry.add(str(source_dir))

    def _raise(path: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(registry_module.shutil, "rmtree", _raise)

    with pytest.raises(RagMonkError):
        registry.remove(source.id)

    # Not partially removed: still registered, and its data untouched.
    assert sources_repo.get(registry._conn, source.id) is not None  # noqa: SLF001
    assert registry.project_dir_for(source).exists()


def test_remove_refuses_while_a_daemon_is_running(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    registry = _registry(tmp_path)
    source = registry.add(str(source_dir))
    home = tmp_path / "home"
    pid.write_pid_file(home, os.getpid())

    with pytest.raises(UsageError, match="daemon"):
        registry.remove(source.id)

    assert sources_repo.get(registry._conn, source.id) is not None  # noqa: SLF001
    assert registry.project_dir_for(source).exists()
