"""``watcher.debounce.Debouncer``: a burst of ``notify()`` calls for the
same key collapses into one ``on_fire`` call after the quiet period;
well-separated notifications each fire on their own.
"""

from __future__ import annotations

import time

from ragmonk.watcher.debounce import Debouncer

_QUIET_MS = 300


def test_rapid_repeated_notifications_collapse_to_one_fire() -> None:
    fired: list[str] = []
    debouncer = Debouncer(_QUIET_MS, fired.append)

    for _ in range(5):
        debouncer.notify("a.py")
        time.sleep(_QUIET_MS / 1000 / 4)  # well inside the quiet window

    time.sleep(_QUIET_MS / 1000 * 3)
    assert fired == ["a.py"]


def test_well_separated_notifications_each_fire() -> None:
    fired: list[str] = []
    debouncer = Debouncer(_QUIET_MS, fired.append)

    debouncer.notify("a.py")
    time.sleep(_QUIET_MS / 1000 * 3)
    debouncer.notify("a.py")
    time.sleep(_QUIET_MS / 1000 * 3)

    assert fired == ["a.py", "a.py"]


def test_different_keys_do_not_interfere() -> None:
    fired: list[str] = []
    debouncer = Debouncer(_QUIET_MS, fired.append)

    debouncer.notify("a.py")
    debouncer.notify("b.py")
    time.sleep(_QUIET_MS / 1000 * 3)

    assert sorted(fired) == ["a.py", "b.py"]


def test_pending_count_reflects_scheduled_and_fired_timers() -> None:
    debouncer = Debouncer(_QUIET_MS, lambda key: None)
    debouncer.notify("a.py")
    assert debouncer.pending_count() == 1
    time.sleep(_QUIET_MS / 1000 * 3)
    assert debouncer.pending_count() == 0


def test_flush_cancels_pending_timers_without_firing() -> None:
    fired: list[str] = []
    debouncer = Debouncer(_QUIET_MS, fired.append)
    debouncer.notify("a.py")
    debouncer.flush()
    time.sleep(_QUIET_MS / 1000 * 3)
    assert fired == []
    assert debouncer.pending_count() == 0
