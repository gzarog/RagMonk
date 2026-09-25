"""Polling-based change detection for network/UNC source roots.

Native filesystem events (inotify/FSEvents/ReadDirectoryChangesW) are not
reliably delivered across a network mount, so these sources are
fingerprinted (path -> (size, mtime), reusing ``sources.scanner.scan``'s
traversal, ignore rules and offline check) on a fixed interval instead of
watched -- see ``core.config.IndexingConfig.network_poll_seconds``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from ragmonk.security.path_guard import PathGuard
from ragmonk.sources.ignore import IgnoreMatcher
from ragmonk.sources.scanner import check_root_accessible, scan

Fingerprint = dict[str, tuple[int, float]]


def fingerprint(
    root: Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    follow_symlinks: bool = False,
) -> Fingerprint:
    """One (size, mtime) snapshot of every file under ``root``.

    Returns an empty fingerprint, rather than raising, when ``root``
    itself is currently unreachable -- an offline network source's
    "nothing changed since I can't see it" state is exactly what
    ``IndexCoordinator.run()`` (the actual re-index this triggers) is
    already responsible for handling correctly; this poller only decides
    *whether* to ask for a re-index, never itself decides what changed.
    """
    if check_root_accessible(root) is not None:
        return {}
    guard = PathGuard([root])
    ignore_matcher = IgnoreMatcher(
        root=root,
        extra_patterns=list(exclude_patterns or []),
        include_patterns=list(include_patterns or []),
    )
    return {
        sf.path: (sf.size, sf.mtime)
        for sf in scan(
            root, guard=guard, ignore_matcher=ignore_matcher, follow_symlinks=follow_symlinks
        )
    }


def diff(previous: Fingerprint, current: Fingerprint) -> set[str]:
    """Paths that appeared, disappeared, or changed size/mtime between
    two fingerprints of the same root.
    """
    changed = {path for path, stat in current.items() if previous.get(path) != stat}
    changed |= previous.keys() - current.keys()
    return changed


class NetworkSourceWatcher:
    """Polls one network source root on a fixed interval and fires
    ``on_trigger`` at most once per tick that finds any change.

    Indexing optimization plan, Phase P2: ``on_trigger`` now receives
    the exact set of changed paths this tick's diff already computed
    (previously it fired with no arguments) -- a poll tick already re-
    fingerprints the whole source to detect a change at all, so handing
    that diff to the caller lets ``IndexCoordinator`` reuse it for a
    targeted pass (finding F2) instead of walking the network tree a
    second time from scratch.
    """

    def __init__(
        self,
        root: Path,
        *,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        interval_seconds: float,
        on_trigger: Callable[[set[str]], None],
        follow_symlinks: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = root
        self._include = list(include_patterns or [])
        self._exclude = list(exclude_patterns or [])
        self._interval = interval_seconds
        self._on_trigger = on_trigger
        self._follow_symlinks = follow_symlinks
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fingerprint: Fingerprint = {}

    def _fingerprint_now(self) -> Fingerprint:
        return fingerprint(
            self._root,
            include_patterns=self._include,
            exclude_patterns=self._exclude,
            follow_symlinks=self._follow_symlinks,
        )

    def poll_once(self) -> bool:
        """Runs one poll tick immediately (no interval wait). Returns
        whether a change was found (and ``on_trigger`` fired). Exposed
        directly so tests -- and a caller wanting an immediate check --
        don't have to wait out a real interval.
        """
        current = self._fingerprint_now()
        changed = diff(self._last_fingerprint, current)
        self._last_fingerprint = current
        if changed:
            self._on_trigger(changed)
            return True
        return False

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.poll_once()

    def start(self) -> None:
        # Seeds the baseline fingerprint without firing a trigger: the
        # daemon has already run (or is about to run) a full index pass
        # for every enabled source on startup, so an "everything looks
        # new" trigger on the very first tick would just be redundant
        # work, not a real signal.
        self._last_fingerprint = self._fingerprint_now()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
