"""Unit tests for ``retrieval/cache.py``: the bounded LRU primitive
itself, and the ``PRAGMA data_version``-based cache key that makes
invalidation automatic (blueprint sections 23/24).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ragmonk.retrieval import cache


def test_bounded_cache_evicts_least_recently_used() -> None:
    c: cache.BoundedCache[str] = cache.BoundedCache(max_size=2)
    c.set("a", "1")
    c.set("b", "2")
    c.get("a")  # "a" is now more recently used than "b"
    c.set("c", "3")  # evicts "b", the least recently used
    assert c.get("a") == "1"
    assert c.get("b") is None
    assert c.get("c") == "3"


def test_bounded_cache_tracks_hits_and_misses() -> None:
    c: cache.BoundedCache[str] = cache.BoundedCache(max_size=10)
    assert c.get("missing") is None
    c.set("k", "v")
    assert c.get("k") == "v"
    assert c.misses == 1
    assert c.hits == 1


def test_search_cache_key_changes_when_a_different_connection_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()

    key_before = cache.search_cache_key(query="q", mode="lexical", limit=10, connections=[conn])

    # A write from a *different* connection is what data_version detects
    # -- mirrors every real writer here (indexing, the daemon) opening
    # its own fresh connection rather than reusing the searching one.
    other = sqlite3.connect(db_path)
    other.execute("INSERT INTO t VALUES (1)")
    other.commit()
    other.close()

    key_after = cache.search_cache_key(query="q", mode="lexical", limit=10, connections=[conn])
    assert key_before != key_after
    conn.close()


def test_search_cache_key_differs_for_different_database_files(tmp_path: Path) -> None:
    conn_a = sqlite3.connect(tmp_path / "a.db")
    conn_b = sqlite3.connect(tmp_path / "b.db")
    key_a = cache.search_cache_key(query="q", mode="lexical", limit=10, connections=[conn_a])
    key_b = cache.search_cache_key(query="q", mode="lexical", limit=10, connections=[conn_b])
    assert key_a != key_b
    conn_a.close()
    conn_b.close()


def test_embedding_cache_key_is_scoped_to_model_and_query_text() -> None:
    k1 = cache.embedding_cache_key(query="hello", model_id="model-a")
    k2 = cache.embedding_cache_key(query="hello", model_id="model-b")
    k3 = cache.embedding_cache_key(query="goodbye", model_id="model-a")
    assert len({k1, k2, k3}) == 3


def test_reset_caches_drops_previous_singleton_instances() -> None:
    first = cache.get_search_result_cache(max_size=5)
    first.set("k", "v")
    cache.reset_caches()
    second = cache.get_search_result_cache(max_size=5)
    assert second.get("k") is None
