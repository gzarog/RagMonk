"""``scan()``'s intra-run dedup: a real user-reported crash on a live
install. A source's file walk can legitimately yield the same real file
twice in one pass -- most plausibly, per the report, a Windows NTFS
junction (invisible to both ``Path.is_symlink()`` and ``os.walk``'s own
``followlinks``, unlike a real symlink) looping back to a directory
already reached by a normal path. Reproduced here with a POSIX symlink
instead (junctions aren't creatable on Linux), which exercises the exact
same code path: two different *as-walked* paths that ``PathGuard.resolve()``
normalizes to one identical real path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ragpilot.security.path_guard import PathGuard
from ragpilot.sources.ignore import IgnoreMatcher
from ragpilot.sources.scanner import scan


def _scan_all(root: Path, *, follow_symlinks: bool) -> list[str]:
    guard = PathGuard([root])
    matcher = IgnoreMatcher(root=root)
    return [
        sf.path
        for sf in scan(root, guard=guard, ignore_matcher=matcher, follow_symlinks=follow_symlinks)
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevated rights")
def test_a_file_reachable_via_two_symlinked_paths_is_yielded_once(tmp_path: Path) -> None:
    root = tmp_path / "source"
    real_dir = root / "real_dir"
    real_dir.mkdir(parents=True)
    (real_dir / "file.txt").write_text("hello")
    (root / "link_to_real_dir").symlink_to(real_dir, target_is_directory=True)

    paths = _scan_all(root, follow_symlinks=True)

    real_file = str((real_dir / "file.txt").resolve())
    assert paths.count(real_file) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevated rights")
def test_a_directory_reachable_via_two_symlinked_paths_is_not_walked_twice(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    real_dir = root / "real_dir"
    real_dir.mkdir(parents=True)
    (real_dir / "a.txt").write_text("a")
    (real_dir / "b.txt").write_text("b")
    (root / "link_one").symlink_to(real_dir, target_is_directory=True)
    (root / "link_two").symlink_to(real_dir, target_is_directory=True)

    paths = _scan_all(root, follow_symlinks=True)

    # Three routes to the same two files (real_dir, link_one, link_two) --
    # each file must still appear exactly once, and the total count must
    # not be inflated to 6.
    assert len(paths) == 2
    assert len(set(paths)) == 2


def test_normal_files_are_unaffected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")

    paths = _scan_all(root, follow_symlinks=False)

    assert len(paths) == 2
    assert len(set(paths)) == 2
