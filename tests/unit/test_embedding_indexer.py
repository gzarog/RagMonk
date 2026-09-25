"""``indexing/embedding_indexer.py`` at the repository level, no CLI/real
embedding model involved (mirrors ``test_embeddings_repo.py``'s style):
proves ``embed_touched_files`` computes and stores one vector per
non-empty entity/document-section subject, and -- Search Quality
Improvement Plan, Phase 3 -- that a document-section subject's embedded
text is the row's stored ``embedding_text`` (document title + heading
path breadcrumb + raw text, computed by ``documents/chunker.py`` and
persisted by ``documents/pipeline.py``), never the raw, context-free
``text`` column.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.models import (
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragmonk.documents import chunker
from ragmonk.indexing import embedding_indexer
from ragmonk.retrieval import embedder
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo, embeddings_repo, entities_repo, files_repo
from ragmonk.storage.sqlite import connect, transaction


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def _seed_document_section(conn, *, embedding_text: str | None) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/tmp/docs/manual.md",
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
                id="d1",
                source_id="s1",
                file_id="f1",
                format=DocumentFormat.MARKDOWN,
                title="Sportsbook Architecture",
                paragraph_count=1,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
        )
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id="p1",
                document_id="d1",
                file_id="f1",
                text="The provider sends a settlement notification.",
                heading_path=["Settlement Processing", "Provider Settlement Flow"],
                order_index=0,
                generation=1,
                created_at="now",
            ),
            doc_title="Sportsbook Architecture",
            embedding_text=embedding_text,
        )


def _seed_code_entity(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id="f2",
            source_id="s1",
            path="/tmp/src/service.py",
            kind=FileKind.CODE,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    with transaction(conn):
        entities_repo.insert(
            conn,
            Entity(
                id="e1",
                source_id="s1",
                file_id="f2",
                kind=EntityType.FUNCTION,
                name="bark_loudly",
                qualified_name="AnimalService.bark_loudly",
                language="python",
                signature="def bark_loudly(self):",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark_loudly(self): ...",
        )


def _fake_embed_texts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake(texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(embedder, "embed_texts", _fake)
    return calls


def test_embeds_stored_embedding_text_not_raw_text(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    contextual = (
        "Document: Sportsbook Architecture\n"
        "Section: Settlement Processing > Provider Settlement Flow\n\n"
        "The provider sends a settlement notification."
    )
    _seed_document_section(conn, embedding_text=contextual)
    calls = _fake_embed_texts(monkeypatch)

    with transaction(conn):
        count = embedding_indexer.embed_touched_files(
            conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
        )

    assert count == 1
    assert calls == [[contextual]]
    # The raw, context-free paragraph text must never be what's embedded.
    assert calls[0][0] != "The provider sends a settlement notification."

    stored = embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
    assert len(stored) == 1
    assert stored[0].subject_id == "p1"


def test_falls_back_to_raw_text_when_embedding_text_is_unset(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """A row written before Phase 3 (or by a caller that never passed
    ``embedding_text``) has it stored as ``NULL``/``""`` -- degrading to
    the raw section text rather than skipping the row or erroring keeps
    such rows embedded (just without the contextual boost) until they are
    next reindexed.
    """
    _seed_document_section(conn, embedding_text=None)
    calls = _fake_embed_texts(monkeypatch)

    with transaction(conn):
        count = embedding_indexer.embed_touched_files(
            conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
        )

    assert count == 1
    assert calls == [["The provider sends a settlement notification."]]


def test_embed_touched_files_stamps_the_reuse_identity_it_just_embedded_with(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """Search Quality Improvement Plan, Phase 12: once vectors are
    actually (re)computed, the touched file's ``embedding_model_id``/
    ``embedding_text_version`` stamp is updated too -- a document-kind
    file gets ``documents/chunker.py``'s ``EMBEDDING_TEXT_VERSION``, a
    code-kind file gets this module's own ``CODE_EMBEDDING_TEXT_VERSION``,
    since the two have distinct, independently-versioned text-assembly
    steps (``Chunk.contextual_text`` vs. ``_entity_text``).
    """
    _seed_document_section(conn, embedding_text="contextual text")
    _seed_code_entity(conn)
    _fake_embed_texts(monkeypatch)

    with transaction(conn):
        count = embedding_indexer.embed_touched_files(
            conn, source_id="s1", touched_code_file_ids=["f2"], touched_document_file_ids=["f1"]
        )
    assert count == 2

    document_file = files_repo.get(conn, "f1")
    assert document_file is not None
    assert document_file.embedding_model_id == embedder.EMBEDDING_MODEL_ID
    assert document_file.embedding_text_version == chunker.EMBEDDING_TEXT_VERSION

    code_file = files_repo.get(conn, "f2")
    assert code_file is not None
    assert code_file.embedding_model_id == embedder.EMBEDDING_MODEL_ID
    assert code_file.embedding_text_version == embedding_indexer.CODE_EMBEDDING_TEXT_VERSION


def test_prepare_embeddings_performs_no_writes(conn, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Indexing optimization plan, Phase P5: ``prepare_embeddings`` is the
    read-plus-model-inference half meant to run *before* a caller opens
    its write transaction -- proving it writes nothing itself is what
    makes that safe to do outside ``BEGIN IMMEDIATE``.
    """
    _seed_document_section(conn, embedding_text="contextual text")
    _fake_embed_texts(monkeypatch)

    prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
    )

    assert prepared is not None
    assert len(prepared.subjects) == 1
    assert embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID) == []
    document_file = files_repo.get(conn, "f1")
    assert document_file is not None
    assert document_file.embedding_model_id is None

    with transaction(conn):
        count = embedding_indexer.publish_embeddings(conn, prepared)
    assert count == 1
    assert len(embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)) == 1


def test_prepare_embeddings_dedupes_identical_texts_within_the_batch(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """Two entities sharing the exact same signature text (e.g. two
    overloads) must only be sent through the model once -- the model
    call receives one text per *unique* string, and both subjects still
    end up with a vector in the result.
    """
    _seed_code_entity(conn)
    with transaction(conn):
        entities_repo.insert(
            conn,
            Entity(
                id="e2",
                source_id="s1",
                file_id="f2",
                kind=EntityType.FUNCTION,
                name="bark_loudly",
                qualified_name="OtherAnimalService.bark_loudly",
                language="python",
                signature="def bark_loudly(self):",  # identical text to e1
                start_line=10,
                end_line=11,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark_loudly(self): ...",
        )
    calls = _fake_embed_texts(monkeypatch)

    prepared = embedding_indexer.prepare_embeddings(
        conn, source_id="s1", touched_code_file_ids=["f2"], touched_document_file_ids=[]
    )

    assert prepared is not None
    assert len(prepared.subjects) == 2
    assert len(prepared.vectors) == 2
    assert calls == [["def bark_loudly(self):"]]  # the model saw it exactly once
    assert prepared.vectors[0] == prepared.vectors[1]


def test_embed_touched_files_leaves_the_stamp_untouched_when_the_model_is_unavailable(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """A skipped embedding step must never claim a rebuild that didn't
    happen -- otherwise a later run would wrongly believe this file's
    vectors are already current and never retry.
    """
    _seed_document_section(conn, embedding_text="contextual text")

    def _raise(texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        raise embedder.EmbeddingModelUnavailableError("simulated")

    monkeypatch.setattr(embedder, "embed_texts", _raise)

    with transaction(conn):
        count = embedding_indexer.embed_touched_files(
            conn, source_id="s1", touched_code_file_ids=[], touched_document_file_ids=["f1"]
        )
    assert count == 0

    document_file = files_repo.get(conn, "f1")
    assert document_file is not None
    assert document_file.embedding_model_id is None
    assert document_file.embedding_text_version is None


def test_multi_file_batch_assigns_entities_and_sections_to_the_correct_file(
    conn,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """Indexing optimization plan V2, Phase P4: ``prepare_embeddings``
    now fetches every touched code file's entities (and every touched
    document file's sections) in one batched query each
    (``entities_repo.list_by_files``/``documents_repo.
    list_units_by_files``) instead of one query per file, then regroups
    them by ``file_id`` in Python. A three-code-file, two-document-file
    batch with distinct, file-identifying text on every subject is the
    regression test for that regrouping: every subject must still land
    on its own originating file, never mixed up with another file's rows
    -- entities_repo.list_by_files/documents_repo.list_units_by_files are
    unit-tested for parity on their own in
    test_storage_batched_writes.py; this is the integration point that
    proves the regrouping built on top of them is correct too.
    """
    for i in range(3):
        fid = f"code{i}"
        files_repo.insert(
            conn,
            FileRecord(
                id=fid,
                source_id="s1",
                path=f"/src/{fid}.py",
                kind=FileKind.CODE,
                size=10,
                mtime=0.0,
                status=FileStatus.QUEUED,
                created_at="now",
                updated_at="now",
            ),
        )
        with transaction(conn):
            entities_repo.insert(
                conn,
                Entity(
                    id=f"{fid}-e",
                    source_id="s1",
                    file_id=fid,
                    kind=EntityType.FUNCTION,
                    name=f"fn_{fid}",
                    qualified_name=f"{fid}.fn_{fid}",
                    language="python",
                    signature=f"def fn_{fid}(): pass  # from {fid}",
                    start_line=1,
                    end_line=2,
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
                snippet="...",
            )
    for i in range(2):
        fid = f"doc{i}"
        files_repo.insert(
            conn,
            FileRecord(
                id=fid,
                source_id="s1",
                path=f"/docs/{fid}.md",
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
                    id=f"d-{fid}",
                    source_id="s1",
                    file_id=fid,
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
                    id=f"{fid}-p",
                    document_id=f"d-{fid}",
                    file_id=fid,
                    text=f"paragraph text unique to {fid}",
                    heading_path=[],
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Doc",
                embedding_text=f"paragraph text unique to {fid}",
            )

    _fake_embed_texts(monkeypatch)
    prepared = embedding_indexer.prepare_embeddings(
        conn,
        source_id="s1",
        touched_code_file_ids=["code0", "code1", "code2"],
        touched_document_file_ids=["doc0", "doc1"],
    )
    assert prepared is not None
    assert len(prepared.subjects) == 5

    by_subject_id = {subject_id: file_id for _kind, subject_id, file_id, _text in prepared.subjects}
    assert by_subject_id == {
        "code0-e": "code0",
        "code1-e": "code1",
        "code2-e": "code2",
        "doc0-p": "doc0",
        "doc1-p": "doc1",
    }
    by_subject_text = {subject_id: text for _kind, subject_id, _file_id, text in prepared.subjects}
    assert by_subject_text["code0-e"].endswith("from code0")
    assert by_subject_text["code2-e"].endswith("from code2")
    assert by_subject_text["doc1-p"] == "paragraph text unique to doc1"

    with transaction(conn):
        count = embedding_indexer.publish_embeddings(conn, prepared)
    assert count == 5

    for fid in ("code0", "code1", "code2", "doc0", "doc1"):
        record = files_repo.get(conn, fid)
        assert record is not None
        assert record.embedding_model_id == embedder.EMBEDDING_MODEL_ID
    for fid in ("code0", "code1", "code2"):
        assert files_repo.get(conn, fid).embedding_text_version == (
            embedding_indexer.CODE_EMBEDDING_TEXT_VERSION
        )
    for fid in ("doc0", "doc1"):
        assert files_repo.get(conn, fid).embedding_text_version == chunker.EMBEDDING_TEXT_VERSION

    stored = {
        e.subject_id: e.file_id
        for e in embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
    }
    assert stored == by_subject_id
