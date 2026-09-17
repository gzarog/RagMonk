"""Unit tests for ``retrieval/ann.py``: both ``AnnIndex`` backends behave
identically from a caller's perspective (add/remove/search/save/load),
and ``select_backend``/``sync_index_for_files``/``rebuild_index`` apply
the blueprint's engine-selection, incremental-update and rebuild rules
(sections 14/15/32/48/49).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ragmonk.core.models import EmbeddingSubjectType
from ragmonk.retrieval import ann
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import embeddings_repo, vector_items_repo
from ragmonk.storage.sqlite import connect, transaction

MODEL = "test-model"


@pytest.fixture(params=["bruteforce", "usearch"])
def index(request: pytest.FixtureRequest, tmp_path: Path) -> ann.AnnIndex:
    if request.param == "bruteforce":
        return ann.BruteForceAnnIndex()
    return ann.USearchAnnIndex(ndim=3, path=tmp_path / "vectors.usearch")


def test_add_and_search_ranks_by_cosine_similarity(index: ann.AnnIndex) -> None:
    index.add([1, 2, 3], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]])
    results = index.search([1.0, 0.0, 0.0], k=3)
    ids = [vector_id for vector_id, _score in results]
    assert ids[0] == 1
    assert ids[-1] == 2
    assert len(index) == 3


def test_remove_drops_the_vector_from_future_searches(index: ann.AnnIndex) -> None:
    index.add([1, 2], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    index.remove([1])
    assert len(index) == 1
    results = index.search([1.0, 0.0, 0.0], k=5)
    assert [vector_id for vector_id, _score in results] == [2]


def test_save_and_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "vectors.usearch"
    written = ann.USearchAnnIndex(ndim=2, path=path)
    written.add([10, 20], [[1.0, 0.0], [0.0, 1.0]])
    written.save()
    assert path.is_file()

    loaded = ann.USearchAnnIndex(ndim=2, path=path)
    loaded.load()
    assert len(loaded) == 2
    results = loaded.search([1.0, 0.0], k=1)
    assert results[0][0] == 10
    assert results[0][1] == pytest.approx(1.0, abs=1e-4)


def test_select_backend_bruteforce_never_imports_usearch() -> None:
    index, backend = ann.select_backend("bruteforce", ndim=4, index_path=Path("/nonexistent"))
    assert backend == "bruteforce"
    assert isinstance(index, ann.BruteForceAnnIndex)


def test_select_backend_auto_prefers_usearch(tmp_path: Path) -> None:
    index, backend = ann.select_backend("auto", ndim=4, index_path=tmp_path / "v.usearch")
    assert backend == "usearch"
    assert isinstance(index, ann.USearchAnnIndex)


def test_get_cached_backend_reuses_instance_when_file_unchanged(tmp_path: Path) -> None:
    index_path = tmp_path / "vectors.usearch"
    written = ann.USearchAnnIndex(ndim=3, path=index_path)
    written.add([1], [[1.0, 0.0, 0.0]])
    written.save()

    first, backend1 = ann.get_cached_backend("auto", ndim=3, index_path=index_path)
    second, backend2 = ann.get_cached_backend("auto", ndim=3, index_path=index_path)
    assert backend1 == "usearch"
    assert backend2 == "usearch"
    assert first is second


def test_get_cached_backend_reloads_after_the_file_changes(tmp_path: Path) -> None:
    index_path = tmp_path / "vectors.usearch"
    written = ann.USearchAnnIndex(ndim=3, path=index_path)
    written.add([1], [[1.0, 0.0, 0.0]])
    written.save()

    first, _ = ann.get_cached_backend("auto", ndim=3, index_path=index_path)
    assert len(first) == 1

    # Simulate a rebuild/sync (in this process or another): more vectors
    # added and saved, with the file's mtime bumped forward by a full
    # second so the assertion is immune to filesystem timestamp
    # granularity -- the cache must notice the on-disk index moved on.
    rebuilt = ann.USearchAnnIndex(ndim=3, path=index_path)
    rebuilt.add([1, 2], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    rebuilt.save()
    stat = index_path.stat()
    os.utime(index_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    second, _ = ann.get_cached_backend("auto", ndim=3, index_path=index_path)
    assert second is not first
    assert len(second) == 2


def test_get_cached_backend_never_caches_bruteforce(tmp_path: Path) -> None:
    index_path = tmp_path / "does-not-exist.usearch"
    first, backend1 = ann.get_cached_backend("bruteforce", ndim=3, index_path=index_path)
    second, backend2 = ann.get_cached_backend("bruteforce", ndim=3, index_path=index_path)
    assert backend1 == "bruteforce"
    assert backend2 == "bruteforce"
    assert first is not second


def _seed_vector_items(conn, *, file_id: str, count: int, model_id: str = MODEL) -> list[int]:
    ids = []
    with transaction(conn):
        for i in range(count):
            subject_id = f"{file_id}-e{i}"
            embeddings_repo.insert(
                conn,
                subject_type=EmbeddingSubjectType.ENTITY,
                subject_id=subject_id,
                file_id=file_id,
                source_id="s1",
                model_id=model_id,
                vector=[float(i), 1.0, 0.0],
            )
            ids.append(
                vector_items_repo.insert(
                    conn,
                    subject_type="entity",
                    subject_id=subject_id,
                    file_id=file_id,
                    source_id="s1",
                    model_id=model_id,
                )
            )
    return ids


def test_sync_index_for_files_builds_index_on_first_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_vector_items(conn, file_id="f1", count=3)

        backend = ann.sync_index_for_files(
            conn,
            project_id="proj1",
            home=home,
            engine="auto",
            ndim=3,
            model_id=MODEL,
            removed_vector_ids=[],
            touched_file_ids=["f1"],
            rebuild_deleted_ratio=0.15,
        )
        assert backend == "usearch"
        index_path = tmp_path / "home" / "projects" / "proj1" / "vectors.usearch"
        assert index_path.is_file()

        index, _ = ann.select_backend("auto", ndim=3, index_path=index_path)
        assert len(index) == 3
    finally:
        conn.close()


def test_sync_index_for_files_removes_and_readds_on_reindex(tmp_path: Path) -> None:
    home = tmp_path / "home"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        old_ids = _seed_vector_items(conn, file_id="f1", count=2)
        ann.sync_index_for_files(
            conn,
            project_id="proj1",
            home=home,
            engine="auto",
            ndim=3,
            model_id=MODEL,
            removed_vector_ids=[],
            touched_file_ids=["f1"],
            rebuild_deleted_ratio=0.15,
        )

        # Simulate a reindex: drop the old generation, add a new one
        # (mirrors embed_touched_files deleting both embeddings and
        # vector_items for a touched file before reinserting).
        embeddings_repo.delete_by_file(conn, "f1")
        vector_items_repo.delete_by_file(conn, "f1")
        new_ids = _seed_vector_items(conn, file_id="f1", count=1)
        backend = ann.sync_index_for_files(
            conn,
            project_id="proj1",
            home=home,
            engine="auto",
            ndim=3,
            model_id=MODEL,
            removed_vector_ids=old_ids,
            touched_file_ids=["f1"],
            rebuild_deleted_ratio=0.15,
        )
        assert backend == "usearch"

        index_path = home / "projects" / "proj1" / "vectors.usearch"
        index, _ = ann.select_backend("auto", ndim=3, index_path=index_path)
        assert len(index) == 1
        results = index.search([0.0, 1.0, 0.0], k=5)
        assert [vector_id for vector_id, _score in results] == new_ids
    finally:
        conn.close()


def test_sync_index_for_files_rebuilds_on_model_change(tmp_path: Path) -> None:
    home = tmp_path / "home"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_vector_items(conn, file_id="f1", count=2, model_id="old-model")
        ann.sync_index_for_files(
            conn,
            project_id="proj1",
            home=home,
            engine="auto",
            ndim=3,
            model_id="old-model",
            removed_vector_ids=[],
            touched_file_ids=["f1"],
            rebuild_deleted_ratio=0.15,
        )

        # A different model: the on-disk index (built for "old-model")
        # is now incompatible and must be rebuilt from scratch, not
        # loaded and mixed with the previous model's vector space.
        _seed_vector_items(conn, file_id="f2", count=1, model_id="new-model")
        backend = ann.sync_index_for_files(
            conn,
            project_id="proj1",
            home=home,
            engine="auto",
            ndim=3,
            model_id="new-model",
            removed_vector_ids=[],
            touched_file_ids=["f2"],
            rebuild_deleted_ratio=0.15,
        )
        assert backend == "usearch"
        meta = ann._read_meta(home / "projects" / "proj1" / "vectors.meta.json")
        assert meta["model_id"] == "new-model"
        assert meta["dim"] == 3
    finally:
        conn.close()


def test_rebuild_index_reflects_current_vector_items(tmp_path: Path) -> None:
    home = tmp_path / "home"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed_vector_items(conn, file_id="f1", count=4)
        backend = ann.rebuild_index(
            conn, project_id="proj1", home=home, engine="auto", ndim=3, model_id=MODEL
        )
        assert backend == "usearch"
        index_path = home / "projects" / "proj1" / "vectors.usearch"
        index, _ = ann.select_backend("auto", ndim=3, index_path=index_path)
        assert len(index) == 4
    finally:
        conn.close()
