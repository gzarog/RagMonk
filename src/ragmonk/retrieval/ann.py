"""Approximate Nearest Neighbor semantic search (blueprint sections
12/30/32): a persistent, incrementally-updated index so ``retrieval/
semantic.py`` no longer scores a query against every current-model
embedding in the project on every search.

``AnnIndex`` is a small protocol two backends satisfy:

* ``USearchAnnIndex`` -- the production backend, backed by the
  ``usearch`` package's HNSW index, persisted to
  ``<project>/vectors.usearch``.
* ``BruteForceAnnIndex`` -- an in-memory fallback with the exact same
  interface, used when ``usearch`` can't be imported (unsupported
  platform, broken install) or when ``search.vector.engine`` is
  explicitly set to ``"bruteforce"``. It has nothing to persist: its
  vectors already live in ``embeddings``/``vector_items`` (SQLite stays
  authoritative either way, per blueprint section 49), so ``save``/
  ``load`` are no-ops.

``hnswlib`` (the blueprint's suggested second choice) is deliberately
not wired in as a third backend: ``usearch`` -> brute-force is already a
two-tier "fast path, always-correct fallback" that never leaves semantic
search unavailable, and maintaining a second native HNSW binding for a
case ``usearch``'s own wheel coverage doesn't already handle would add a
real maintenance surface for no observed gap (see the package's PyPI
wheel matrix: linux/macOS/Windows, x86_64/arm64, cp310-cp314). The
``AnnIndex`` protocol keeps that swap possible later without touching
any caller.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from ragmonk.core import paths
from ragmonk.storage.repositories import vector_items_repo
from ragmonk.telemetry.logging import get_logger, log_event

_logger = get_logger("ann")

_META_FORMAT_VERSION = 1


class AnnIndex(Protocol):
    def add(self, ids: Sequence[int], vectors: Sequence[Sequence[float]]) -> None: ...

    def remove(self, ids: Sequence[int]) -> None: ...

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]: ...

    def save(self) -> None: ...

    def load(self) -> None: ...

    def __len__(self) -> int: ...


class BruteForceAnnIndex:
    """Pure-Python fallback: an in-memory ``{id: vector}`` map scored via
    ``retrieval/vectorstore.py``'s cosine similarity. No persistence of
    its own -- every process that needs it calls ``load()`` (a no-op)
    and then repopulates it from ``vector_items_repo.list_all_with_vectors``
    before use, the same "rebuild from SQLite" contract a fresh
    ``USearchAnnIndex`` load falls back to on a version mismatch.
    """

    def __init__(self) -> None:
        self._vectors: dict[int, list[float]] = {}

    def add(self, ids: Sequence[int], vectors: Sequence[Sequence[float]]) -> None:
        for vector_id, vector in zip(ids, vectors, strict=True):
            self._vectors[vector_id] = list(vector)

    def remove(self, ids: Sequence[int]) -> None:
        for vector_id in ids:
            self._vectors.pop(vector_id, None)

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        from ragmonk.retrieval.vectorstore import top_k

        candidates = [(str(vid), vec) for vid, vec in self._vectors.items()]
        ranked = top_k(vector, candidates, k=k)
        return [(int(c.key), c.score) for c in ranked]

    def save(self) -> None:
        pass

    def load(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self._vectors)


class USearchAnnIndex:
    """HNSW index via the ``usearch`` package. Vectors are always
    L2-normalized before storage (``retrieval/embedder.py``), so cosine
    similarity is used as the metric and ``score = 1 - distance`` (that
    identity is what ``usearch``'s ``metric="cos"`` distance actually
    returns for normalized vectors).
    """

    def __init__(self, *, ndim: int, path: Path) -> None:
        self._ndim = ndim
        self._path = path
        self._index = self._new_index()

    def _new_index(self) -> object:
        from usearch.index import Index

        return Index(ndim=self._ndim, metric="cos", dtype="f32")

    def add(self, ids: Sequence[int], vectors: Sequence[Sequence[float]]) -> None:
        if not ids:
            return
        import numpy as np

        keys = np.array(ids, dtype=np.int64)
        data = np.array(vectors, dtype=np.float32)
        self._index.add(keys, data)  # type: ignore[attr-defined]

    def remove(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        import numpy as np

        self._index.remove(np.array(ids, dtype=np.int64))  # type: ignore[attr-defined]

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        if len(self._index) == 0:  # type: ignore[arg-type]
            return []
        import numpy as np

        matches = self._index.search(np.array(vector, dtype=np.float32), k)  # type: ignore[attr-defined]
        return [
            (int(key), 1.0 - float(distance))
            for key, distance in zip(matches.keys, matches.distances, strict=True)
        ]

    def save(self) -> None:
        """Atomic write (blueprint section 49): saved to a temp file in
        the same directory, then renamed into place, so a crash mid-save
        can never leave a half-written index that a later ``load()``
        would misread -- ``os.replace`` is atomic on every platform this
        project supports (POSIX rename, Windows ``MoveFileEx`` with
        replace).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        self._index.save(str(tmp_path))  # type: ignore[attr-defined]
        os.replace(tmp_path, self._path)

    def load(self) -> None:
        if not self._path.is_file():
            return
        fresh = self._new_index()
        fresh.load(str(self._path))  # type: ignore[attr-defined]
        self._index = fresh

    def __len__(self) -> int:
        return len(self._index)  # type: ignore[arg-type]


def select_backend(
    engine: str, *, ndim: int, index_path: Path, load: bool = True
) -> tuple[AnnIndex, str]:
    """Auto engine selection (blueprint section 32): ``"auto"``/
    ``"usearch"`` try to import and construct ``usearch``'s ``Index``,
    falling back to ``BruteForceAnnIndex`` on any failure (missing
    package, incompatible native build) rather than ever raising --
    semantic search must stay available even when the ANN package
    itself is broken. ``"bruteforce"`` skips straight to the fallback.

    ``load=False`` skips reading any existing on-disk index -- used when
    the caller is about to rebuild it from scratch and reading the stale
    file first would be wasted work (or, worse, leave old entries mixed
    in if the rebuild doesn't fully overwrite the in-memory index).
    """
    if engine == "bruteforce":
        return BruteForceAnnIndex(), "bruteforce"

    try:
        index = USearchAnnIndex(ndim=ndim, path=index_path)
        if load:
            index.load()
        return index, "usearch"
    except Exception as exc:  # noqa: BLE001 - any import/native-load failure -> fall back
        if engine == "usearch":
            log_event(
                _logger,
                "usearch_unavailable_falling_back",
                level=logging.WARNING,
                error=str(exc),
            )
        return BruteForceAnnIndex(), "bruteforce"


# Process-local cache of a *loaded* on-disk index, keyed by
# (index_path, engine, ndim) -> (index_file's mtime_ns when loaded,
# AnnIndex, backend). One-shot CLI processes populate and immediately
# discard this on exit, same as retrieval/cache.py's caches -- the real
# beneficiary is a long-lived process serving repeated searches
# (``ragmonk serve``), for which reloading ``vectors.usearch`` from
# disk on every single semantic_search() call would otherwise undo the
# blueprint's "daemon keeps ANN indexes in memory" goal (section 25).
# Unbounded: unlike a query-result cache, this is keyed by project
# index-file identity, which is bounded by how many sources a process
# ever searches, not by arbitrary query text.
_index_cache_lock = threading.Lock()
_index_cache: dict[tuple[str, str, int], tuple[int, AnnIndex, str]] = {}


def _index_mtime_ns(index_path: Path) -> int:
    try:
        return index_path.stat().st_mtime_ns
    except OSError:
        return -1


def get_cached_backend(engine: str, *, ndim: int, index_path: Path) -> tuple[AnnIndex, str]:
    """Read-only counterpart to ``select_backend`` for the *search* path
    only (``retrieval/semantic.py``): reuses a previously loaded index
    for this exact ``(index_path, engine, ndim)`` as long as the on-disk
    file's mtime hasn't changed since it was cached, instead of
    re-reading and re-deserializing it from disk on every call.

    Never used by the write path (``rebuild_index``/``sync_index_for_files``):
    those already load once, mutate, and save within a single call --
    there is nothing to cache there, and reusing a cached instance would
    risk mutating a copy some concurrent search is still reading.

    Only a ``"usearch"`` result actually gets cached. A ``"bruteforce"``
    result -- whether ``engine`` was explicitly ``"bruteforce"``, or
    ``select_backend`` fell back internally because ``usearch`` itself
    couldn't load -- has no on-disk file of its own to key a cache on:
    its caller reseeds it from ``vector_items`` on every call when
    empty, and ``BruteForceAnnIndex.save()`` is a no-op that never
    touches ``index_path``, so caching it here by that path's mtime
    would freeze whatever it was first seeded with across every later
    call in this process, even as ``vector_items`` keeps changing.

    Invalidation: every writer finishes with ``USearchAnnIndex.save``'s
    atomic tmp-file-then-``os.replace``, which always bumps the file's
    mtime, so a cache entry from before a rebuild/sync (in this process
    or another) never gets served after one -- the next call's mtime
    check misses and reloads.
    """
    key = (str(index_path), engine, ndim)
    current_mtime = _index_mtime_ns(index_path)
    with _index_cache_lock:
        cached = _index_cache.get(key)
        if cached is not None and cached[0] == current_mtime:
            return cached[1], cached[2]

    index, backend = select_backend(engine, ndim=ndim, index_path=index_path)
    if backend != "usearch":
        return index, backend

    with _index_cache_lock:
        _index_cache[key] = (current_mtime, index, backend)
    return index, backend


def _read_meta(meta_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(meta_path: Path, **fields: Any) -> None:
    """Atomic write, mirroring ``USearchAnnIndex.save``'s tmp+rename --
    the index file and its metadata should never be individually
    torn/partial, even if they can still drift from each other across a
    hard crash between the two writes (acceptable: a mismatched
    ``model_id``/``dim`` on the next read just forces a rebuild, per
    blueprint section 48, never a silent wrong-vector-space read).
    """
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format_version": _META_FORMAT_VERSION, **fields}
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, meta_path)


def is_index_compatible(meta_path: Path, *, model_id: str, ndim: int) -> bool:
    """Whether the on-disk index at ``meta_path``'s project was built for
    this exact ``model_id``/``ndim`` -- checked before a *read* (``load``
    would otherwise try to deserialize a different-dimension index into
    a freshly constructed one of the current ``ndim``, which is a crash,
    not a graceful mismatch) rather than after, unlike the write-side
    rebuild-on-mismatch check in ``sync_index_for_files``.
    """
    meta = _read_meta(meta_path)
    return meta is not None and meta.get("model_id") == model_id and meta.get("dim") == ndim


def rebuild_index(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    home: Path,
    engine: str,
    ndim: int,
    model_id: str,
) -> str:
    """Rebuilds the project's ANN index from ``vector_items``/
    ``embeddings`` from scratch (blueprint section 15's ``ragmonk
    vectors rebuild``) -- also the path a first-ever sync or a model
    change falls into, since SQLite is always the authoritative source
    an ANN index can be regenerated from (section 49). Returns the
    backend name actually used.
    """
    index_path = paths.project_vector_index_path(project_id, home)
    meta_path = paths.project_vector_meta_path(project_id, home)
    index, backend = select_backend(engine, ndim=ndim, index_path=index_path, load=False)
    all_vectors = vector_items_repo.list_all_with_vectors(conn, model_id=model_id)
    if all_vectors:
        index.add([vid for vid, _ in all_vectors], [vec for _, vec in all_vectors])
    index.save()
    _write_meta(
        meta_path,
        model_id=model_id,
        dim=ndim,
        metric="cosine",
        backend=backend,
        removed_since_rebuild=0,
    )
    return backend


def sync_index_for_files(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    home: Path,
    engine: str,
    ndim: int,
    model_id: str,
    removed_vector_ids: Sequence[int],
    touched_file_ids: Sequence[str],
    rebuild_deleted_ratio: float,
) -> str:
    """Applies one source pass's vector changes to the project's
    persistent ANN index (blueprint section 14): removes the touched
    files' previous-generation vector ids, adds back their current ones,
    and persists. Falls through to a full ``rebuild_index`` when no
    compatible on-disk index exists yet (first run, or the embedding
    model changed -- section 48's "never silently use stale vectors"),
    or when the fraction of vectors removed since the last rebuild
    exceeds ``rebuild_deleted_ratio`` (section 15's automatic
    optimization policy, guarding against HNSW graph fragmentation from
    many small incremental deletes). Returns the backend name actually
    used.
    """
    index_path = paths.project_vector_index_path(project_id, home)
    meta_path = paths.project_vector_meta_path(project_id, home)
    meta = _read_meta(meta_path)
    compatible = meta is not None and meta.get("model_id") == model_id and meta.get("dim") == ndim
    if not compatible:
        return rebuild_index(
            conn, project_id=project_id, home=home, engine=engine, ndim=ndim, model_id=model_id
        )
    assert meta is not None  # narrows for mypy: `compatible` already required it

    index, backend = select_backend(engine, ndim=ndim, index_path=index_path)
    index.remove(removed_vector_ids)
    added = vector_items_repo.list_with_vectors_by_file(conn, touched_file_ids, model_id=model_id)
    if added:
        index.add([vid for vid, _ in added], [vec for _, vec in added])
    index.save()

    removed_since_rebuild = int(meta.get("removed_since_rebuild", 0)) + len(removed_vector_ids)
    total_vectors = vector_items_repo.count_all(conn, model_id=model_id)
    if total_vectors > 0 and removed_since_rebuild / total_vectors > rebuild_deleted_ratio:
        return rebuild_index(
            conn, project_id=project_id, home=home, engine=engine, ndim=ndim, model_id=model_id
        )

    _write_meta(
        meta_path,
        model_id=model_id,
        dim=ndim,
        metric="cosine",
        backend=backend,
        removed_since_rebuild=removed_since_rebuild,
    )
    return backend
