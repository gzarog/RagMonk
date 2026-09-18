"""Query result and query-embedding caching (blueprint sections 23/24).

Both caches are process-local (plain module-level state): ``ragmonk
search``/``ragmonk explore`` are one-shot CLI invocations, a fresh
process per call, so an in-memory cache there is created and discarded
with nothing to show for it. The actual beneficiary is any process that
serves *repeated* queries within its own lifetime -- ``ragmonk serve``
(the MCP server) chief among them, since the same query is often
repeated by different MCP clients/agents against one running server
(see the blueprint's own "CLI / MCP / agents / ChatGPT / Claude / Hermes"
example). Wired into ``retrieval/lexical.py``'s ``search_with_timings``
and ``retrieval/semantic.py``'s ``semantic_search``.

Invalidation is automatic rather than an explicit "clear" call: every
cache key embeds each searched project's on-disk database identity and
its SQLite ``PRAGMA data_version`` -- a free, O(1) built-in counter that
changes whenever that database file is committed to by a *different*
connection (every ``ragmonk index``/MCP-tool-call/daemon pass opens its
own fresh connection, so this reliably fires across processes and
across each MCP tool call's own fresh ``AppContext``). A cache entry
keyed on a since-changed data_version simply never matches again; it
just ages out of the bounded LRU like any other cold entry, so there is
no separate invalidation path to keep in sync with every writer.

Known limitation, not exercised by any code path in this project today:
``PRAGMA data_version`` does not change for a write made by the *same*
connection that then re-reads it (only writes from other connections are
visible this way) -- safe for every current caller here, which always
either opens a fresh connection per call (MCP tools, CLI commands) or
never writes on the connection it searches with, but would need a
different signal for a hypothetical future long-lived process that both
indexes and serves searches on the same cached connection.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import OrderedDict
from typing import Any


class BoundedCache[V]:
    """A tiny thread-safe LRU cache. Not ``functools.lru_cache``: the key
    here is computed from live database state (see module docstring),
    not derived automatically from a function's own arguments.
    """

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self._data: OrderedDict[str, V] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> V | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                self.misses += 1
                return None
            self.hits += 1
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def _connection_identity(conn: sqlite3.Connection) -> str:
    """``<absolute file path>@<data_version>`` for one connection's main
    database -- the file path disambiguates distinct databases (two
    different projects, or two test fixtures, can share a logical
    ``source_id``), and ``data_version`` disambiguates two reads of the
    *same* database across time.
    """
    file_path = conn.execute("PRAGMA database_list").fetchone()[2]
    data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    return f"{file_path}@{data_version}"


def search_cache_key(
    *, query: str, mode: str, limit: int, connections: list[sqlite3.Connection], extra: str = ""
) -> str:
    """Blueprint section 23's cache key: query + search mode + generation
    + source set, hashed to a fixed-length string. ``connections`` should
    be every project connection the search will actually touch.
    """
    fingerprint = ",".join(sorted(_connection_identity(conn) for conn in connections))
    raw = f"{query}\x1f{mode}\x1f{limit}\x1f{fingerprint}\x1f{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def embedding_cache_key(*, query: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\x1f{query}".encode()).hexdigest()


_search_result_cache: BoundedCache[Any] | None = None
_query_embedding_cache: BoundedCache[list[float]] | None = None


def get_search_result_cache(max_size: int) -> BoundedCache[Any]:
    global _search_result_cache
    if _search_result_cache is None:
        _search_result_cache = BoundedCache(max_size)
    return _search_result_cache


def get_query_embedding_cache(max_size: int) -> BoundedCache[list[float]]:
    global _query_embedding_cache
    if _query_embedding_cache is None:
        _query_embedding_cache = BoundedCache(max_size)
    return _query_embedding_cache


def reset_caches() -> None:
    """Drops both process-global caches entirely (not just their
    contents) -- used by the test suite so each test starts from a clean
    slate the same way a fresh process would (see module docstring), and
    available to any long-lived caller that wants to force a full reset
    rather than wait for entries to age out.
    """
    global _search_result_cache, _query_embedding_cache
    _search_result_cache = None
    _query_embedding_cache = None
