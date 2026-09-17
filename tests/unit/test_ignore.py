from __future__ import annotations

from pathlib import Path

from ragmonk.sources.ignore import IgnoreMatcher


def test_default_excluded_dir_is_ignored(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(root=tmp_path)
    assert matcher.is_ignored(tmp_path / "node_modules", is_dir=True) is True


def test_secret_filename_is_ignored(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(root=tmp_path)
    assert matcher.is_ignored(tmp_path / ".env", is_dir=False) is True
    assert matcher.is_ignored(tmp_path / "id_rsa", is_dir=False) is True


def test_ordinary_file_is_not_ignored(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(root=tmp_path)
    assert matcher.is_ignored(tmp_path / "main.py", is_dir=False) is False


def test_gitignore_patterns_are_applied(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\nbuild_output/\n")
    matcher = IgnoreMatcher(root=tmp_path)
    assert matcher.is_ignored(tmp_path / "debug.log", is_dir=False) is True
    assert matcher.is_ignored(tmp_path / "build_output", is_dir=True) is True
    assert matcher.is_ignored(tmp_path / "keep.py", is_dir=False) is False


def test_ragmonkignore_patterns_are_applied(tmp_path: Path) -> None:
    (tmp_path / ".ragmonkignore").write_text("secretdata/\n")
    matcher = IgnoreMatcher(root=tmp_path)
    assert matcher.is_ignored(tmp_path / "secretdata", is_dir=True) is True


def test_nested_file_under_default_excluded_dir_is_ignored(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(root=tmp_path)
    nested = tmp_path / ".git" / "objects" / "abcd"
    assert matcher.is_ignored(nested, is_dir=False) is True


def test_include_pattern_overrides_exclude(tmp_path: Path) -> None:
    matcher = IgnoreMatcher(
        root=tmp_path, extra_patterns=["*.log"], include_patterns=["keep.log"]
    )
    assert matcher.is_ignored(tmp_path / "keep.log", is_dir=False) is False
    assert matcher.is_ignored(tmp_path / "other.log", is_dir=False) is True
