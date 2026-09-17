"""Content hashing and the mtime+size fast-skip used to avoid rehashing
files that almost certainly have not changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 1 << 20
_MTIME_EPSILON = 1e-6


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def stat_unchanged(prev_size: int, prev_mtime: float, cur_size: int, cur_mtime: float) -> bool:
    return prev_size == cur_size and abs(prev_mtime - cur_mtime) < _MTIME_EPSILON
