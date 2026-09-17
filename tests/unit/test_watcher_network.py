"""``watcher.network``: fingerprint/diff logic and the polling loop's
interval, exercised against a plain local directory -- no real network
mount is needed, since polling only depends on ``os.stat``-level
information the scanner already reuses.
"""

from __future__ import annotations

import time
from pathlib import Path

from ragmonk.watcher.network import NetworkSourceWatcher, diff, fingerprint


def test_fingerprint_of_offline_root_is_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert fingerprint(missing) == {}


def test_fingerprint_captures_size_and_mtime(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    fp = fingerprint(tmp_path)
    assert len(fp) == 1
    path, (size, mtime) = next(iter(fp.items()))
    assert path.endswith("a.txt")
    assert size == len("hello")
    assert mtime > 0


def test_diff_detects_added_removed_and_changed() -> None:
    previous = {"a": (10, 1.0), "b": (5, 2.0)}
    current = {"a": (10, 1.0), "b": (6, 2.0), "c": (1, 3.0)}
    assert diff(previous, current) == {"b", "c"}


def test_diff_of_identical_fingerprints_is_empty() -> None:
    fp = {"a": (10, 1.0)}
    assert diff(fp, dict(fp)) == set()


def test_poll_once_seeds_baseline_but_start_does_not_trigger_on_first_tick(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hello")
    triggered = []
    watcher = NetworkSourceWatcher(
        tmp_path, interval_seconds=60, on_trigger=lambda: triggered.append(True)
    )
    watcher.start()
    try:
        time.sleep(0.2)
        assert triggered == []  # baseline seeded silently, no spurious trigger
    finally:
        watcher.stop(timeout=5.0)


def test_poll_once_detects_a_change_made_between_polls(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    triggered = []
    watcher = NetworkSourceWatcher(
        tmp_path, interval_seconds=60, on_trigger=lambda: triggered.append(True)
    )
    watcher.start()
    try:
        (tmp_path / "b.txt").write_text("new file")
        assert watcher.poll_once() is True
        assert triggered == [True]
        assert watcher.poll_once() is False  # nothing changed since the last poll
    finally:
        watcher.stop(timeout=5.0)


def test_background_loop_respects_the_configured_interval(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    triggered = []
    interval = 0.2
    watcher = NetworkSourceWatcher(
        tmp_path, interval_seconds=interval, on_trigger=lambda: triggered.append(time.monotonic())
    )
    watcher.start()
    try:
        (tmp_path / "b.txt").write_text("new file")
        # Before roughly one interval has elapsed, the background loop
        # should not have ticked yet.
        time.sleep(interval * 0.4)
        assert triggered == []

        deadline = time.monotonic() + interval * 5
        while time.monotonic() < deadline and not triggered:
            time.sleep(0.02)
        assert len(triggered) == 1
    finally:
        watcher.stop(timeout=5.0)


def test_stop_joins_the_background_thread(tmp_path: Path) -> None:
    watcher = NetworkSourceWatcher(tmp_path, interval_seconds=0.05, on_trigger=lambda: None)
    watcher.start()
    assert watcher.is_alive
    watcher.stop(timeout=5.0)
    assert not watcher.is_alive
