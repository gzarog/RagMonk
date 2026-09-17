"""Native OS filesystem-event watching (inotify/FSEvents/
ReadDirectoryChangesW, via ``watchdog``) for one local source root.

Translates raw filesystem events into "this file needs reindexing"
trigger calls, debounced by ``watcher.debounce.Debouncer`` so a burst of
writes to the same file (a save, a `git checkout`, an editor's atomic
write-then-rename) collapses into exactly one trigger instead of one per
underlying OS event.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ragmonk.watcher.debounce import Debouncer


class _TriggerHandler(FileSystemEventHandler):
    """Forwards every file-level create/modify/delete/move event to the
    debouncer, keyed by the affected path. Directory events are ignored
    entirely -- the daemon re-triggers a full source rescan
    (``IndexCoordinator.run()``) regardless of which specific file
    changed, so a directory-level event carries no information a
    contained file event doesn't already provide, and forwarding it too
    would only double-count the same underlying change.
    """

    def __init__(self, debouncer: Debouncer) -> None:
        self._debouncer = debouncer

    def on_created(self, event: FileSystemEvent) -> None:
        self._notify(event.src_path, event.is_directory)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._notify(event.src_path, event.is_directory)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._notify(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._notify(event.src_path, event.is_directory)
        dest = getattr(event, "dest_path", None)
        if dest:
            self._notify(dest, event.is_directory)

    def _notify(self, path: str | bytes, is_directory: bool) -> None:
        if is_directory:
            return
        self._debouncer.notify(path if isinstance(path, str) else path.decode())


class LocalSourceWatcher:
    """One ``watchdog`` ``Observer`` scoped to a single local source root.

    ``on_trigger`` fires per touched file (already debounced), not once
    per source -- the daemon only needs "a re-index of this source is
    due", so any of its triggers can be coalesced further upstream, but
    keeping per-file granularity here costs nothing and leaves room for a
    future per-file fast path without revisiting this class.
    """

    def __init__(
        self,
        root: Path,
        *,
        debounce_ms: int,
        on_trigger: Callable[[Path], None],
        recursive: bool = True,
    ) -> None:
        self._root = root
        self._on_trigger = on_trigger
        self._debouncer = Debouncer(debounce_ms, self._fire)
        self._observer = Observer()
        self._observer.schedule(_TriggerHandler(self._debouncer), str(root), recursive=recursive)

    def _fire(self, key: str) -> None:
        self._on_trigger(Path(key))

    def start(self) -> None:
        self._observer.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        # Pending debounced triggers are dropped, not flushed early: a
        # graceful shutdown must not start new indexing work, and
        # anything genuinely missed is exactly what the periodic
        # reconciliation pass (service/daemon.py) exists to catch on the
        # next daemon start.
        self._debouncer.flush()
        self._observer.stop()
        self._observer.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._observer.is_alive()
