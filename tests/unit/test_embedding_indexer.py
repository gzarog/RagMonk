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

from ragpilot.core.models import (
    Document,
    DocumentFormat,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragpilot.indexing import embedding_indexer
from ragpilot.retrieval import embedder
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo, embeddings_repo, files_repo
from ragpilot.storage.sqlite import connect, transaction


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


def _fake_embed_texts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(embedder, "embed_texts", _fake)
    return calls


def test_embeds_stored_embedding_text_not_raw_text(
    conn, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
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
    conn, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
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
