from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ragmonk.core.errors import SecurityViolationError
from ragmonk.security.path_guard import PathGuard


def test_legitimate_nested_path_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "a" / "b" / "c.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")
    guard = PathGuard([root])
    assert guard.resolve(nested) == nested.resolve()


def test_relative_traversal_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    guard = PathGuard([root])
    escaping = root / ".." / "outside.txt"
    with pytest.raises(SecurityViolationError):
        guard.resolve(escaping)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated privileges on Windows")
def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside_target = tmp_path / "outside"
    outside_target.mkdir()
    (outside_target / "secret.txt").write_text("secret")

    link = root / "escape"
    link.symlink_to(outside_target, target_is_directory=True)

    guard = PathGuard([root])
    with pytest.raises(SecurityViolationError):
        guard.resolve(link / "secret.txt")


def test_security_violation_maps_to_exit_code_8(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    guard = PathGuard([root])
    try:
        guard.resolve(tmp_path / "elsewhere.txt")
    except SecurityViolationError as exc:
        assert exc.exit_code == 8
    else:
        pytest.fail("expected SecurityViolationError")
