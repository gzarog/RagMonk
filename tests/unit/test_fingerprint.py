from __future__ import annotations

from pathlib import Path

from ragmonk.sources.fingerprint import FileIdentity, hash_file, stat_unchanged, verified_hash


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


# -- verified_hash (indexing optimization plan, Phase P3 / finding F4) ------


def test_verified_hash_with_no_expected_always_hashes(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    assert verified_hash(file_path, expected=None) == hash_file(file_path)


def test_verified_hash_reuses_expected_when_stat_matches(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    stat = file_path.stat()
    expected = FileIdentity(
        content_hash="not-the-real-hash", size=stat.st_size, mtime=stat.st_mtime
    )

    # A deliberately wrong hash is returned unchanged -- proves this is
    # trusting `expected` (no re-read), not coincidentally recomputing
    # the same real hash.
    assert verified_hash(file_path, expected=expected) == "not-the-real-hash"


def test_verified_hash_rehashes_when_size_differs(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    stat = file_path.stat()
    stale = FileIdentity(content_hash="stale", size=stat.st_size + 1, mtime=stat.st_mtime)
    assert verified_hash(file_path, expected=stale) == hash_file(file_path)


def test_verified_hash_rehashes_when_mtime_differs(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    stat = file_path.stat()
    stale = FileIdentity(content_hash="stale", size=stat.st_size, mtime=stat.st_mtime + 100)
    assert verified_hash(file_path, expected=stale) == hash_file(file_path)


def test_verified_hash_falls_back_when_file_is_gone(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello world")
    stat = file_path.stat()
    expected = FileIdentity(content_hash="whatever", size=stat.st_size, mtime=stat.st_mtime)
    file_path.unlink()
    try:
        verified_hash(file_path, expected=expected)
    except OSError:
        pass  # hash_file itself raising on a missing file is the expected fallback path
    else:
        raise AssertionError("expected an OSError from hashing a missing file")
