"""``scan()``'s ``outcome`` parameter (indexing optimization plan, Phase
P1 / finding F6): an unreadable subtree must be reported, not silently
swallowed the way plain ``os.walk`` (no ``onerror``) does -- see
``scanner.py``'s module docstring and ``ScanOutcome``'s own docstring.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ragmonk.security.path_guard import PathGuard
from ragmonk.sources.ignore import IgnoreMatcher
from ragmonk.sources.scanner import ScanOutcome, scan

_skip_unreadable_dir_test = sys.platform == "win32" or (
    hasattr(os, "geteuid") and os.geteuid() == 0
)


def _scan_all(root: Path, *, outcome: ScanOutcome) -> list[str]:
    guard = PathGuard([root])
    ignore_matcher = IgnoreMatcher(root=root, extra_patterns=[], include_patterns=[])
    return [
        sf.path for sf in scan(root, guard=guard, ignore_matcher=ignore_matcher, outcome=outcome)
    ]


def test_fully_readable_tree_reports_no_errors(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y = 2\n")

    outcome = ScanOutcome()
    paths = _scan_all(tmp_path, outcome=outcome)

    assert outcome.complete
    assert outcome.errors == []
    assert len(paths) == 2


def test_outcome_defaults_to_complete_when_never_populated() -> None:
    outcome = ScanOutcome()
    assert outcome.complete is True


@pytest.mark.skipif(
    _skip_unreadable_dir_test,
    reason="POSIX permission bits only, and root ignores directory permission bits",
)
def test_unreadable_subtree_is_reported_but_other_files_still_scanned(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("x = 1\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("y = 2\n")
    locked.chmod(0o000)
    try:
        outcome = ScanOutcome()
        paths = _scan_all(tmp_path, outcome=outcome)

        assert not outcome.complete
        assert len(outcome.errors) >= 1
        assert any("locked" in e.path for e in outcome.errors)
        # The unreadable subtree's own files are missing, but a sibling
        # file elsewhere in the tree was still found -- an incomplete
        # scan degrades gracefully rather than losing everything.
        assert any(p.endswith("visible.py") for p in paths)
        assert not any(p.endswith("hidden.py") for p in paths)
    finally:
        locked.chmod(0o755)


def test_scan_without_outcome_argument_still_works(tmp_path: Path) -> None:
    """Backward compatibility: every pre-P1 caller that doesn't pass
    ``outcome`` at all must keep working exactly as before.
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    guard = PathGuard([tmp_path])
    ignore_matcher = IgnoreMatcher(root=tmp_path, extra_patterns=[], include_patterns=[])
    paths = [sf.path for sf in scan(tmp_path, guard=guard, ignore_matcher=ignore_matcher)]
    assert len(paths) == 1
