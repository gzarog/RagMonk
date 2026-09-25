"""Indexing optimization plan V2, Phase P3: persistent, project-local
embedding reuse (``storage/repositories/embedding_cache_repo.py``,
wired into ``indexing/embedding_indexer.py``'s ``prepare_embeddings``/
``publish_embeddings``).

Mirrors ``test_embedding_indexer.py``'s seeding style and its
``_fake_embed_texts`` spy convention, no real embedding model involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.models import (
    Document,
    DocumentFormat,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragmonk.indexing import embedding_indexer
from ragmonk.retrieval import embedder
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import (
    documents_repo,
    embedding_cache_repo,
    embeddings_repo,
    files_repo,
)
from ragmonk.storage.sqlite import connect, transaction
from ragmonk.tokenization import model_identity


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def _seed_document_section(conn, *, file_id: str = "f1", text: str) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id=file_id,
            source_id="s1",
            path=f"/tmp/docs/{file_id}.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    with transaction(conn):
        documents_repo.insert_document(
            conn,
            Document(
                id=f"d-{file_id}",
                source_id="s1",
                file_id=file_id,
                format=DocumentFormat.MARKDOWN,
                title="Doc",
                paragraph_count=1,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
        )
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id=f"p-{file_id}",
                document_id=f"d-{file_id}",
                file_id=file_id,
                text=text,
                heading_path=[],
                order_index=0,
                generation=1,
                created_at="now",
            ),
            doc_title="Doc",
            embedding_text=text,
        )


def _fake_embed_texts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake(texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(embedder, "embed_texts", _fake)
    return calls


def _embed_once(conn, *, file_id: str = "f1") -> None:  # noqa: ANN001
    prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=[file_id]
    )
    assert prepared is not None
    with transaction(conn):
        embedding_indexer.publish_embeddings(conn, prepared)


def test_persistent_reuse_across_two_separate_runs(conn, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Two separate ``prepare_embeddings``/``publish_embeddings`` passes
    (mirroring two separate ``run_source_pass`` calls, e.g. a file
    re-touched by an embedding-version-stale reprocess with unchanged
    text) embedding the exact same text must call the model only once
    total.
    """
    _seed_document_section(conn, text="identical contextual text")
    calls = _fake_embed_texts(monkeypatch)

    _embed_once(conn)
    assert calls == [["identical contextual text"]]

    # Second pass: same file, same text (as if re-touched for an
    # unrelated reason, e.g. an embeddings-stale reprocess) -- must not
    # call the model again.
    _embed_once(conn)
    assert calls == [["identical contextual text"]]  # no second call

    stored = embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
    assert len(stored) == 1
    assert stored[0].vector == [len("identical contextual text"), 1.0]
    assert embedding_cache_repo.count_all(conn) == 1


def test_reuse_invalidation_on_preprocessing_version_change(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tokenizer/preprocessing identity change (the same axis Exact
    Tokenizer plan Phase 4 already tracks for documents) must prevent
    reuse of a vector cached under the old identity -- the model runs
    again rather than serving a vector that may no longer correspond to
    how this text would tokenize now.
    """
    _seed_document_section(conn, text="text under old tokenizer")
    calls = _fake_embed_texts(monkeypatch)

    _embed_once(conn)
    assert len(calls) == 1

    monkeypatch.setattr(model_identity, "TOKENIZER_REVISION", "deadbeef" * 5)

    _embed_once(conn)
    assert len(calls) == 2  # recomputed, not reused

    # Two distinct preprocessing_version rows now coexist in the cache --
    # nothing was overwritten, since they're genuinely different keys.
    assert embedding_cache_repo.count_all(conn) == 2


def test_reuse_invalidation_on_model_id_change(conn, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """A model-id change must equally prevent unsafe reuse -- model id is
    one of the cache key's four axes, independent of preprocessing.
    """
    _seed_document_section(conn, text="text under old model")
    calls = _fake_embed_texts(monkeypatch)

    _embed_once(conn)
    assert len(calls) == 1

    monkeypatch.setattr(embedder, "EMBEDDING_MODEL_ID", "some-other-model")

    _embed_once(conn)
    assert len(calls) == 2  # recomputed, not reused


def test_project_boundary_embedding_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different projects -- two different ``knowledge.db`` files,
    exactly how this project actually isolates projects (``core/lifecycle.
    AppContext.project_conn``) -- must never share a cached vector even
    when they embed the exact same text under the exact same model/
    preprocessing/version identity. Project A's cache write must be
    physically invisible to project B's connection.
    """
    conn_a = connect(tmp_path / "project_a" / "knowledge.db")
    conn_b = connect(tmp_path / "project_b" / "knowledge.db")
    try:
        apply_migrations(conn_a, "knowledge")
        apply_migrations(conn_b, "knowledge")

        _seed_document_section(conn_a, text="shared text, two projects")
        _seed_document_section(conn_b, text="shared text, two projects")
        calls = _fake_embed_texts(monkeypatch)

        _embed_once(conn_a)
        assert len(calls) == 1
        assert embedding_cache_repo.count_all(conn_a) == 1
        assert embedding_cache_repo.count_all(conn_b) == 0

        # Project B embedding the identical text must still call the
        # model -- project A's cache row is in a different database file
        # entirely and cannot be reached from conn_b.
        _embed_once(conn_b)
        assert len(calls) == 2
        assert embedding_cache_repo.count_all(conn_b) == 1

        stored_a = embeddings_repo.list_by_model(conn_a, embedder.EMBEDDING_MODEL_ID)
        stored_b = embeddings_repo.list_by_model(conn_b, embedder.EMBEDDING_MODEL_ID)
        assert len(stored_a) == 1 and len(stored_b) == 1
    finally:
        conn_a.close()
        conn_b.close()


def test_within_batch_dedup_still_applies_on_top_of_the_cache(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3 layers cache reuse *on top of* the existing within-batch dedup
    (Phase P5) -- two subjects sharing identical text in the *same* batch
    still cost exactly one model call (a cache miss), not two, and both
    still end up with the same vector, exactly like before this phase.
    """
    _seed_document_section(conn, file_id="f1", text="same text twice")
    _seed_document_section(conn, file_id="f2", text="same text twice")
    calls = _fake_embed_texts(monkeypatch)

    prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1", "f2"]
    )
    assert prepared is not None
    assert len(prepared.subjects) == 2
    assert calls == [["same text twice"]]  # one unique text -> one call

    with transaction(conn):
        count = embedding_indexer.publish_embeddings(conn, prepared)
    assert count == 2
    stored = embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
    assert len(stored) == 2
    assert stored[0].vector == stored[1].vector


def test_interrupted_publication_rolls_back_cache_and_vectors_together(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through ``publish_embeddings``'s write
    transaction must roll back the ``embedding_cache`` upsert right along
    with the ``embeddings``/``vector_items`` rows it was derived
    alongside -- never leaving a cache row for a vector that was never
    actually published (SQLite's own atomicity already guarantees this
    once the write is entirely inside one transaction, exactly like every
    other generational write in this project). A subsequent, uninterrupted
    retry must then converge to exactly one full, consistent generation.
    """
    _seed_document_section(conn, text="interrupted publication text")
    _fake_embed_texts(monkeypatch)

    prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
    )
    assert prepared is not None

    from ragmonk.storage.repositories import vector_items_repo

    real_insert = vector_items_repo.insert
    calls = {"n": 0}

    def _flaky_insert(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        raise RuntimeError("simulated crash mid-publish")

    monkeypatch.setattr(vector_items_repo, "insert", _flaky_insert)

    with pytest.raises(RuntimeError), transaction(conn):
        embedding_indexer.publish_embeddings(conn, prepared)

    # Nothing committed -- embeddings, vector_items and the cache all
    # rolled back together.
    assert embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID) == []
    assert embedding_cache_repo.count_all(conn) == 0
    document_file = files_repo.get(conn, "f1")
    assert document_file is not None
    assert document_file.embedding_model_id is None

    # Retry, uninterrupted: converges to exactly one consistent
    # generation.
    monkeypatch.setattr(vector_items_repo, "insert", real_insert)
    retry_prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
    )
    assert retry_prepared is not None
    with transaction(conn):
        count = embedding_indexer.publish_embeddings(conn, retry_prepared)
    assert count == 1
    assert len(embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)) == 1
    assert embedding_cache_repo.count_all(conn) == 1
