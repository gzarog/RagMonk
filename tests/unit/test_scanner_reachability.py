"""``check_root_accessible`` -- the fix that lets a caller tell "the
source root itself is unreachable" apart from "the root is reachable and
genuinely empty", which ``os.walk`` alone (what ``scan()`` is built on)
cannot do (see the module docstring in ``sources/scanner.py``).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from ragmonk.sources.scanner import check_root_accessible


def test_accessible_root_returns_none(tmp_path: Path) -> None:
    assert check_root_accessible(tmp_path) is None


def test_accessible_but_empty_root_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_root_accessible(empty) is None


def test_missing_root_is_not_accessible(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert check_root_accessible(missing) is not None


def test_root_that_is_a_file_not_a_directory_is_not_accessible(tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    assert check_root_accessible(a_file) is not None


def test_deleted_root_flips_from_accessible_to_not(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    assert check_root_accessible(root) is None

    shutil.rmtree(root)
    assert check_root_accessible(root) is not None


# Two stacked skipif marks each independently evaluate their condition
# expression at module-collection time, on every platform, regardless of
# the other mark -- os.geteuid() doesn't exist on Windows, so evaluating
# it there crashes collection instead of skipping. Short-circuit into one
# expression so it's never called off-POSIX.
_skip_unreadable_root_test = sys.platform == "win32" or (
    hasattr(os, "geteuid") and os.geteuid() == 0
)


@pytest.mark.skipif(
    _skip_unreadable_root_test,
    reason="POSIX permission bits only, and root ignores directory permission bits",
)
def test_unreadable_root_is_not_accessible(tmp_path: Path) -> None:
    root = tmp_path / "locked"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    root.chmod(0o000)
    try:
        assert check_root_accessible(root) is not None
    finally:
        root.chmod(0o755)
