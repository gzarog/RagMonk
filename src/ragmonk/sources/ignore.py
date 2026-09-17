"""Merges default excludes, secret-filename rules, .gitignore/.ragmonkignore
and per-source include/exclude patterns into a single matcher.

This is a pragmatic gitignore-style matcher (fnmatch-based glob per path
segment or full relative path), not a full re-implementation of git's
pattern language -- Phase 1 only needs correct handling of the common
cases exercised by real projects and by our own tests.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ragmonk.security.secrets import is_secret_filename

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".ragmonk", "node_modules", "bin", "obj", "dist",
        "build", "target", ".venv", "venv", "vendor", "coverage",
        ".cache", ".next", ".idea", ".vscode",
    }
)


def _read_pattern_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


@dataclass
class IgnoreMatcher:
    root: Path
    extra_patterns: list[str] = field(default_factory=list)
    include_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        patterns = list(self.extra_patterns)
        patterns += _read_pattern_file(self.root / ".gitignore")
        patterns += _read_pattern_file(self.root / ".ragmonkignore")
        self._patterns = patterns

    def _matches_any(self, patterns: list[str], rel_posix: str, name: str, is_dir: bool) -> bool:
        for pattern in patterns:
            pat = pattern
            dir_only = pat.endswith("/")
            if dir_only:
                pat = pat.rstrip("/")
                if not is_dir:
                    continue
            if "/" in pat:
                if fnmatch.fnmatch(rel_posix, pat.lstrip("/")):
                    return True
            else:
                if fnmatch.fnmatch(name, pat):
                    return True
        return False

    def is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            rel = path
        rel_posix = PurePosixPath(rel).as_posix()
        name = path.name

        if is_dir and name in DEFAULT_EXCLUDE_DIRS:
            return True
        if any(part in DEFAULT_EXCLUDE_DIRS for part in rel.parts[:-1]):
            return True
        if not is_dir and is_secret_filename(name):
            return True
        if not self._matches_any(self._patterns, rel_posix, name, is_dir):
            return False
        return not (
            self.include_patterns
            and self._matches_any(self.include_patterns, rel_posix, name, is_dir)
        )
