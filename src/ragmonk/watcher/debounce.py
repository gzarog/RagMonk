"""Collapses a burst of repeated per-key notifications into one trigger,
fired a quiet period after the *last* notification for that key.

Reuses ``indexing.debounce_ms`` (Phase 1's config field) as its quiet
period rather than adding a parallel setting -- there is exactly one
"how long to wait for a burst of writes to settle" knob in RagMonk,
whether the burst came from a local watchdog event or a network poll
tick; see ``watcher/local.py`` and ``service/daemon.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class Debouncer:
    """Per-key debounce timer.

    Each ``notify(key)`` call (re)starts a ``quiet_ms``-long timer for
    that key; a key's ``on_fire`` callback runs only once a full quiet
    period has passed with no further ``notify`` for it -- so N rapid
    writes to the same file collapse into exactly one trigger, while
    events for *different* keys never interfere with each other's timer.

    Threading: both the local (watchdog) and network watchers deliver
    events from background threads, so every method here must be safe to
    call concurrently with the ``threading.Timer`` callbacks it starts.
    """

    def __init__(
        self,
        quiet_ms: int,
        on_fire: Callable[[str], None],
    ) -> None:
        self._quiet_seconds = max(quiet_ms, 0) / 1000.0
        self._on_fire = on_fire
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    def notify(self, key: str) -> None:
        with self._lock:
            existing = self._timers.get(key)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._quiet_seconds, self._fire, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: str) -> None:
        with self._lock:
            self._timers.pop(key, None)
        self._on_fire(key)

    def flush(self) -> None:
        """Cancels every pending timer without firing it.

        Used on shutdown: a debounced trigger not yet due is discarded
        rather than fired mid-shutdown, since the daemon is about to stop
        accepting new triggers anyway (see ``service/daemon.py``'s
        graceful-shutdown handling) and the periodic reconciliation pass
        will catch it on the daemon's next start.
        """
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._timers)
