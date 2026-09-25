"""Content hashing and the mtime+size fast-skip used to avoid rehashing
files that almost certainly have not changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FileIdentity:
    """A content hash plus the exact ``(size, mtime)`` it was verified
    against -- indexing optimization plan, Phase P3 / finding F4.
    ``IndexCoordinator`` computes this once per scanned file (the same
    hash ``classify_change`` already needed); threading it through
    ``ProcessorContext`` lets downstream stages (the document pipeline,
    the PDF conversion cache) reuse it via ``verified_hash`` below
    instead of each independently re-reading and re-hashing the whole
    file, which is what happened before this phase.
    """

    content_hash: str
    size: int
    mtime: float


def verified_hash(
    path: Path, *, expected: FileIdentity | None, algorithm: str = "sha256"
) -> str:
    """Returns a content hash for ``path``, reusing ``expected.
    content_hash`` when the file's *current* size/mtime still match
    ``expected`` (one cheap ``stat()``, no re-read of the file) --
    otherwise falls back to a full ``hash_file`` re-read, exactly as if
    no identity had been supplied at all.

    ``expected=None`` (any caller with no coordinator-verified
    identity -- a direct processor call, most unit tests) always hashes,
    matching every pre-P3 caller's behavior unchanged. This is
    deliberately a stat comparison, not blind trust in ``expected``: a
    file that was rewritten between the coordinator's scan and this
    call (a real, if narrow, race) must never have its new content
    silently attributed to the old hash.
    """
    if expected is not None:
        try:
            current = path.stat()
        except OSError:
            return hash_file(path, algorithm)
        if stat_unchanged(expected.size, expected.mtime, current.st_size, current.st_mtime):
            return expected.content_hash
    return hash_file(path, algorithm)
