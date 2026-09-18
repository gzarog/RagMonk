"""Request dependencies wiring the UI to RagMonk's application context.

Admin UI plan, Phase 1 (§4.5): reuse ``AppContext.bootstrap()`` -- do not
invent a new storage abstraction for the UI. One context is bootstrapped
at app startup and closed at shutdown (§4.5 "ensure all connections are
properly closed during shutdown").

Concurrency: RagMonk's SQLite connections are not safe for concurrent use
across threads, and Uvicorn runs sync endpoints in a threadpool. Because
this is a single-user, localhost admin tool, DB-touching requests are
serialized through one process lock (:func:`get_ctx`) rather than
bootstrapping a fresh context -- and reopening a log file handle -- on
every request. Endpoints that only touch process/pid/health files (daemon
controls) and the SSE stream deliberately do **not** take that lock, so a
30-second daemon start or a long-lived event stream never blocks the rest
of the UI.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from starlette.requests import Request

from ragmonk.core.lifecycle import AppContext


def get_ctx(request: Request) -> Iterator[AppContext]:
    """Yield the shared ``AppContext`` while holding the DB lock.

    Used by every route that reads or writes RagMonk's SQLite databases,
    so those routes never touch a connection concurrently.
    """
    ctx: AppContext = request.app.state.ctx
    lock: threading.Lock = request.app.state.db_lock
    with lock:
        yield ctx


def get_ctx_unlocked(request: Request) -> AppContext:
    """Return the shared context without taking the DB lock.

    Only safe for callers that touch process/pid/health *files* and
    immutable config -- never the SQLite connections. Used by the daemon
    control routes, whose start/stop poll can run for many seconds.
    """
    return request.app.state.ctx
