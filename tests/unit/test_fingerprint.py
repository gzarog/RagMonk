from __future__ import annotations

from pathlib import Path

from ragmonk.sources.fingerprint import hash_file, stat_unchanged


def test_hash_file_is_deterministic(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    assert hash_file(file_path) == hash_file(file_path)


def test_hash_file_changes_with_content(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    first = hash_file(file_path)
    file_path.write_text("hello!")
    assert hash_file(file_path) != first


def test_stat_unchanged_true_for_identical_stat() -> None:
    assert stat_unchanged(100, 123.456, 100, 123.456) is True


def test_stat_unchanged_false_for_size_change() -> None:
    assert stat_unchanged(100, 123.456, 101, 123.456) is False


def test_stat_unchanged_false_for_mtime_change() -> None:
    assert stat_unchanged(100, 123.456, 100, 200.0) is False


def test_stat_unchanged_tolerates_float_noise() -> None:
    assert stat_unchanged(100, 123.4560001, 100, 123.4560002) is True
