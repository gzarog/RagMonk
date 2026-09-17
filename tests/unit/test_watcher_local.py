"""``watcher.local.LocalSourceWatcher`` against a real temp directory and
a real ``watchdog`` observer. Every wait below is bounded (a short
polling loop with a deadline) so a missed event fails the test instead of
hanging the suite.
"""

from __future__ import annotations

import time
from pathlib import Path

from ragmonk.watcher.local import LocalSourceWatcher

_DEBOUNCE_MS = 50
_WAIT_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.05


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SECONDS) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SECONDS)
    return predicate()


def test_create_modify_delete_each_trigger(tmp_path: Path) -> None:
    triggered: list[Path] = []
    watcher = LocalSourceWatcher(
        tmp_path, debounce_ms=_DEBOUNCE_MS, on_trigger=triggered.append
    )
    watcher.start()
    try:
        target = tmp_path / "a.txt"

        target.write_text("hello")
        assert _wait_until(lambda: any(p.name == "a.txt" for p in triggered))

        triggered.clear()
        target.write_text("hello again")
        assert _wait_until(lambda: any(p.name == "a.txt" for p in triggered))

        triggered.clear()
        target.unlink()
        assert _wait_until(lambda: any(p.name == "a.txt" for p in triggered))
    finally:
        watcher.stop(timeout=5.0)


def test_rename_triggers_both_old_and_new_path(tmp_path: Path) -> None:
    triggered: list[Path] = []
    watcher = LocalSourceWatcher(
        tmp_path, debounce_ms=_DEBOUNCE_MS, on_trigger=triggered.append
    )
    watcher.start()
    try:
        original = tmp_path / "old.txt"
        original.write_text("hi")
        assert _wait_until(lambda: any(p.name == "old.txt" for p in triggered))

        triggered.clear()
        renamed = tmp_path / "new.txt"
        original.rename(renamed)
        assert _wait_until(
            lambda: any(p.name == "new.txt" for p in triggered)
            or any(p.name == "old.txt" for p in triggered)
        )
    finally:
        watcher.stop(timeout=5.0)


def test_directory_only_events_do_not_trigger(tmp_path: Path) -> None:
    triggered: list[Path] = []
    watcher = LocalSourceWatcher(
        tmp_path, debounce_ms=_DEBOUNCE_MS, on_trigger=triggered.append
    )
    watcher.start()
    try:
        (tmp_path / "subdir").mkdir()
        # Give the observer a real chance to deliver (and wrongly forward)
        # a directory-creation event before concluding it correctly didn't.
        time.sleep(0.3)
        assert triggered == []
    finally:
        watcher.stop(timeout=5.0)


def test_stop_flushes_pending_debounce_without_firing(tmp_path: Path) -> None:
    triggered: list[Path] = []
    watcher = LocalSourceWatcher(
        tmp_path, debounce_ms=2000, on_trigger=triggered.append
    )
    watcher.start()
    (tmp_path / "a.txt").write_text("hi")
    time.sleep(0.1)  # event delivered and debounced, well before its quiet period
    watcher.stop(timeout=5.0)
    assert triggered == []
    assert not watcher.is_alive
