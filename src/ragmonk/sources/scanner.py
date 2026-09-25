"""Walks a source root and yields candidate files, honoring ignore rules
and refusing to cross the allowed root via symlinks or traversal.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ragmonk.core.errors import SecurityViolationError
from ragmonk.core.models import ScannedFile
from ragmonk.security.path_guard import PathGuard
from ragmonk.sources.ignore import IgnoreMatcher


@dataclass(frozen=True)
class ScanError:
    """One subtree/file this pass could not fully see -- a directory
    ``os.walk`` could not list (permission denied, a race with a
    delete, an unmounted remote path mid-walk) or a file whose
    ``stat()`` failed after being listed. Either way, this run's scan
    result cannot be trusted as "every file under root, no more no
    less": indexing optimization plan finding F6.
    """

    path: str
    message: str


@dataclass
class ScanOutcome:
    """Mutated in place while ``scan()``'s generator is consumed --
    complete and accurate only once the caller has fully exhausted the
    iterator, exactly like ``result.scanned`` on ``IndexRunResult``
    (``indexing/coordinator.py``). Passed in by the caller (rather than
    returned) so ``scan()`` can stay a plain generator of
    ``ScannedFile`` -- its existing, most-used shape -- while still
    reporting completeness to the one caller that needs it.
    """

    errors: list[ScanError] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors


def check_root_accessible(root: Path) -> str | None:
    """Returns ``None`` if ``root`` is currently reachable (exists, is a
    directory, and its entries can be listed), else a short reason.

    ``os.walk()`` -- what ``scan()`` below is built on -- silently yields
    nothing for a root it cannot list (its default ``onerror`` is a
    no-op): to a caller only watching ``scan()``'s output, an unmounted
    network share is indistinguishable from a directory that is
    genuinely empty. This performs the one real syscall ``os.walk``
    itself skips (listing the root) so a caller can tell those two cases
    apart *before* trusting ``scan()``'s result for deletion
    reconciliation -- see ``indexing/coordinator.py``.
    """
    try:
        resolved = root.resolve()
    except OSError as exc:
        return f"cannot resolve path: {exc}"
    if not resolved.exists():
        return "path does not exist"
    if not resolved.is_dir():
        return "path is not a directory"
    try:
        with os.scandir(resolved) as entries:
            next(iter(entries), None)
    except OSError as exc:
        return f"cannot list directory: {exc}"
    return None


def scan(
    root: Path,
    *,
    guard: PathGuard,
    ignore_matcher: IgnoreMatcher,
    follow_symlinks: bool = False,
    outcome: ScanOutcome | None = None,
) -> Iterator[ScannedFile]:
    """``outcome`` (indexing optimization plan, Phase P1 / finding F6):
    when given, is mutated in place with one ``ScanError`` per
    directory ``os.walk`` could not list (its default ``onerror`` is a
    no-op -- see ``check_root_accessible``'s own docstring above for
    why that matters) and per file whose ``stat()`` failed after being
    listed. ``outcome.complete`` is only meaningful once this generator
    has been fully consumed; the caller (``indexing/coordinator.py``)
    must not trust a scan with errors for deletion reconciliation --
    an unreadable subtree must never look like "these files are gone".

    ``seen_dirs``/``seen_files`` (resolved, real paths, not the
    as-walked ones) guard against the same real file being yielded more
    than once in a single pass -- which ``IndexCoordinator.run()``
    (``indexing/coordinator.py``) would otherwise misclassify as "new"
    twice and crash on ``files.source_id, files.path``'s UNIQUE
    constraint on the second insert. This is not just defensive: on
    Windows, an NTFS junction (``mklink /J``, common in
    repo-sync/build-artifact layouts) is invisible to both
    ``Path.is_symlink()`` (the check just below) and ``os.walk``'s own
    ``followlinks`` -- neither recognizes ``IO_REPARSE_TAG_MOUNT_POINT``
    the way they do a real symlink -- so with ``follow_symlinks=False``
    (the default) a junction still gets walked into. If it loops back to
    a directory already reached by a normal path (a real report against
    a live install), pruning ``seen_dirs`` here catches it at the
    directory level -- before ``os.walk`` ever descends a second time --
    which also bounds what would otherwise be unbounded recursion for a
    junction that loops back to one of its own ancestors.
    """
    root = root.resolve()
    seen_dirs: set[str] = {str(root)}
    seen_files: set[str] = set()

    def _onerror(exc: OSError) -> None:
        if outcome is not None:
            outcome.errors.append(
                ScanError(path=exc.filename or str(root), message=str(exc))
            )

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=follow_symlinks, onerror=_onerror
    ):
        current = Path(dirpath)
        kept_dirs = []
        for dirname in dirnames:
            candidate = current / dirname
            if not follow_symlinks and candidate.is_symlink():
                continue
            if ignore_matcher.is_ignored(candidate, is_dir=True):
                continue
            try:
                resolved_dir = guard.resolve(candidate)
            except SecurityViolationError:
                continue
            resolved_dir_str = str(resolved_dir)
            if resolved_dir_str in seen_dirs:
                continue
            seen_dirs.add(resolved_dir_str)
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            candidate = current / filename
            if not follow_symlinks and candidate.is_symlink():
                continue
            if ignore_matcher.is_ignored(candidate, is_dir=False):
                continue
            try:
                resolved = guard.resolve(candidate)
            except SecurityViolationError:
                continue
            resolved_str = str(resolved)
            if resolved_str in seen_files:
                continue
            seen_files.add(resolved_str)
            try:
                stat = resolved.stat()
            except OSError as exc:
                if outcome is not None:
                    outcome.errors.append(ScanError(path=resolved_str, message=str(exc)))
                continue
            yield ScannedFile(path=resolved_str, size=stat.st_size, mtime=stat.st_mtime)
