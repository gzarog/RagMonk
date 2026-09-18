"""Confines all filesystem access to configured source roots.

Symlinks are resolved before the containment check, so a symlink that
lives inside a root but points outside it is caught the same way a
``../`` traversal is: the resolved path simply no longer lives under any
allowed root.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.errors import SecurityViolationError


class PathGuard:
    def __init__(self, roots: list[Path]) -> None:
        self._roots = [root.resolve() for root in roots]

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def resolve(self, path: Path) -> Path:
        resolved = path.resolve()
        for root in self._roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise SecurityViolationError(
            f"path '{path}' resolves to '{resolved}', which escapes all allowed source roots"
        )
