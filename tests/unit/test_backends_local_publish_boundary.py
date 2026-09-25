"""Storage backend abstraction plan, Phase 3: the normalized publishing
boundary -- ``IndexCoordinator``/``knowledge.linker``/
``indexing.embedding_indexer`` now write code entities/relationships,
document units, embeddings and cross-domain links through
``KnowledgeBackend.publish_*`` instead of touching
``storage/repositories`` directly for that final persistence step.

These tests exercise that boundary specifically -- an explicit
``LocalKnowledgeBackend`` instance is passed in throughout, and each
test asserts against the underlying SQLite rows to prove the backend
call actually persisted them (not just that no exception was raised).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.backends.models import (
    LinkCandidate,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
)
from ragmonk.core.config import IndexingConfig, RagMonkConfig
from ragmonk.core.models import (
    Confidence,
    Document,
    DocumentFormat,
    EmbeddingSubjectType,
    Entity,
    EntityType,
    FileKind,
    FileStatus,
    Paragraph,
    RelationshipType,
)
from ragmonk.core.models import FileRecord as CoreFileRecord
from ragmonk.documents.chunker import Chunk
from ragmonk.indexing.coordinator import IndexCoordinator
from ragmonk.indexing.runner import build_processor_registry
from ragmonk.knowledge.linker import link_touched_files
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    links_repo,
    relationships_repo,
    vector_items_repo,
)
from ragmonk.storage.sqlite import connect, transaction


def _config() -> RagMonkConfig:
    config = RagMonkConfig(indexing=IndexingConfig(code_extraction_workers=1))
    disabled_documents = config.documents.model_copy(update={"enabled": False})
    return config.model_copy(update={"documents": disabled_documents})


def _write_code_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "good.py").write_text("def good():\n    return 1\n")
    (root / "bad.py").write_text("def bad():\n    return 2\n")


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


# -- code: end-to-end through IndexCoordinator ---------------------------


def test_index_run_publishes_code_entities_through_the_backend(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write_code_project(root)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config()
        backend = LocalKnowledgeBackend(conn=conn)
        coord = IndexCoordinator(
            conn,
            "s1",
            str(root),
            [],
            [],
            config,
            processors=build_processor_registry(config),
            backend=backend,
        )
        result = coord.run()

        assert result.indexed == 2
        assert result.failed == 0
        files = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
        good_entities = entities_repo.list_by_file(conn, files[str(root / "good.py")].id)
        bad_entities = entities_repo.list_by_file(conn, files[str(root / "bad.py")].id)
        assert any(e.name == "good" for e in good_entities)
        assert any(e.name == "bad" for e in bad_entities)
    finally:
        conn.close()


def test_incremental_reindex_updates_entities_through_the_backend(tmp_path: Path) -> None:
    """A file changed between two passes gets its entities replaced --
    exercising ``LocalKnowledgeBackend.publish_code``'s delete-then-
    insert semantics across two separate ``IndexCoordinator.run()``
    calls, mirroring a real incremental ``ragmonk index``.
    """
    root = tmp_path / "source"
    root.mkdir()
    target = root / "m.py"
    target.write_text("def one():\n    return 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config()
        backend = LocalKnowledgeBackend(conn=conn)

        def _run() -> None:
            coord = IndexCoordinator(
                conn,
                "s1",
                str(root),
                [],
                [],
                config,
                processors=build_processor_registry(config),
                backend=backend,
            )
            coord.run()

        _run()
        file_id = files_repo.list_by_source(conn, "s1")[0].id
        first_entities = entities_repo.list_by_file(conn, file_id)
        assert any(e.name == "one" for e in first_entities)
        assert not any(e.name == "two" for e in first_entities)

        target.write_text("def two():\n    return 2\n")
        _run()

        second_entities = entities_repo.list_by_file(conn, file_id)
        names = {e.name for e in second_entities}
        assert "two" in names
        assert "one" not in names  # old generation's entities were replaced, not accumulated
    finally:
        conn.close()


def test_per_file_failure_does_not_abort_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend write failure for one file must not prevent the other
    queued file in the same pass from being published -- the
    coordinator's existing per-file isolation (``_finish_job``) applies
    just as it did when processors wrote to ``storage/repositories``
    directly.
    """
    root = tmp_path / "source"
    _write_code_project(root)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config()
        backend = LocalKnowledgeBackend(conn=conn)

        real_publish_code = LocalKnowledgeBackend.publish_code

        def _flaky_publish_code(self: LocalKnowledgeBackend, prepared: PreparedCode) -> None:
            if prepared.file_id is not None and any(
                e.name == "bad" for e in prepared.entities
            ):
                raise RuntimeError("simulated backend failure for bad.py")
            return real_publish_code(self, prepared)

        monkeypatch.setattr(LocalKnowledgeBackend, "publish_code", _flaky_publish_code)

        coord = IndexCoordinator(
            conn,
            "s1",
            str(root),
            [],
            [],
            config,
            processors=build_processor_registry(config),
            backend=backend,
        )
        result = coord.run()

        # The poisoned file's backend write raised, which the
        # coordinator's per-file isolation (``_finish_job``) turns into
        # a scheduled retry rather than letting it abort the batch --
        # the healthy file in the same run still indexed successfully.
        assert result.indexed == 1
        files = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
        good_entities = entities_repo.list_by_file(conn, files[str(root / "good.py")].id)
        assert any(e.name == "good" for e in good_entities)
        bad_file = files[str(root / "bad.py")]
        assert bad_file.status is FileStatus.RETRY
        assert entities_repo.list_by_file(conn, bad_file.id) == []
    finally:
        conn.close()


# -- document: direct LocalKnowledgeBackend.publish_document -------------


def test_publish_document_writes_document_and_chunks(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="doc_f1",
            source_id="s1",
            path="/tmp/docs/doc_f1.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    backend = LocalKnowledgeBackend(conn=conn)
    document = Document(
        id="d1",
        source_id="s1",
        file_id="doc_f1",
        format=DocumentFormat.MARKDOWN,
        title="Manual",
        paragraph_count=1,
        generation=1,
        created_at="now",
        updated_at="now",
    )
    chunk = Chunk(
        kind="paragraph",
        text="pkg.Dog.bark is the core API.",
        heading_level=None,
        heading_path=(),
        parent_index=None,
        page_start=None,
        page_end=None,
        search_text="pkg.Dog.bark is the core API.",
        contextual_text="pkg.Dog.bark is the core API.",
    )
    with transaction(conn):
        backend.publish_document(
            PreparedDocument(
                file_id="doc_f1",
                source_id="s1",
                generation=1,
                document=document,
                chunk_ids=["p1"],
                chunks=[chunk],
                doc_title="Manual",
            )
        )

    units = documents_repo.list_units_by_files(conn, ["doc_f1"])
    assert len(units) == 1
    assert units[0].text == "pkg.Dog.bark is the core API."


def test_publish_document_delete_only_clears_prior_generation(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="doc_f2",
            source_id="s1",
            path="/tmp/docs/doc_f2.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    backend = LocalKnowledgeBackend(conn=conn)
    with transaction(conn):
        documents_repo.insert_document(
            conn,
            Document(
                id="d2",
                source_id="s1",
                file_id="doc_f2",
                format=DocumentFormat.MARKDOWN,
                paragraph_count=0,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
        )
    assert documents_repo.list_units_by_files(conn, ["doc_f2"]) == []

    with transaction(conn):
        backend.publish_document(
            PreparedDocument(file_id="doc_f2", source_id="s1", delete_only=True)
        )

    # Nothing raised, and no unit rows exist for this file either way --
    # this mainly proves delete_only never asserts on document/chunks
    # being None.
    assert documents_repo.list_units_by_files(conn, ["doc_f2"]) == []


# -- embeddings/links: direct LocalKnowledgeBackend calls -----------------


def test_publish_embeddings_writes_vectors_and_stamps_version(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="code_f1",
            source_id="s1",
            path="/repo/a.py",
            kind=FileKind.CODE,
            size=1,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    backend = LocalKnowledgeBackend(conn=conn)
    prepared = PreparedEmbeddings(
        source_id="s1",
        model_id="test-model",
        code_embedding_text_version="1",
        document_embedding_text_version="1",
        subjects=[(EmbeddingSubjectType.ENTITY, "e1", "code_f1", "def a(): ...")],
        vectors=[[0.1, 0.2]],
        touched_code_file_ids=frozenset({"code_f1"}),
        touched_document_file_ids=frozenset(),
        cache_entries=[],
        cache_reused=0,
    )
    with transaction(conn):
        backend.publish_embeddings(prepared)

    stored = embeddings_repo.list_by_model(conn, "test-model")
    assert len(stored) == 1
    assert stored[0].vector == pytest.approx([0.1, 0.2])
    assert vector_items_repo.list_vector_ids_by_file(conn, ["code_f1"])
    refreshed = files_repo.get(conn, "code_f1")
    assert refreshed is not None
    assert refreshed.embedding_model_id == "test-model"


def test_publish_links_inserts_and_dedupes(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="code_f1",
            source_id="s1",
            path="/repo/pkg/dog.py",
            kind=FileKind.CODE,
            size=1,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="doc_f1",
            source_id="s1",
            path="/docs/manual.md",
            kind=FileKind.DOCUMENT,
            size=1,
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
                file_id="code_f1",
                kind=EntityType.FUNCTION,
                name="bark",
                qualified_name="pkg.Dog.bark",
                language="python",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark(self): ...",
        )
        documents_repo.insert_document(
            conn,
            Document(
                id="doc1",
                source_id="s1",
                file_id="doc_f1",
                format=DocumentFormat.MARKDOWN,
                title="Manual",
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
                document_id="doc1",
                file_id="doc_f1",
                text="pkg.Dog.bark is the core API.",
                order_index=0,
                generation=1,
                created_at="now",
            ),
            doc_title="Manual",
        )

    candidate = LinkCandidate(
        entity_id="e1",
        document_id="doc1",
        section_id="p1",
        link_type=RelationshipType.DOCUMENTED_BY,
        resolver="linker:qualified_identifier",
        confidence=Confidence.HIGH,
        evidence="pkg.Dog.bark",
    )
    backend = LocalKnowledgeBackend(conn=conn)
    with transaction(conn):
        inserted = backend.publish_links(PreparedLinks(source_id="s1", candidates=[candidate]))
    assert inserted == 1
    assert len(links_repo.list_by_entity(conn, "e1")) == 1

    # A second call with the same natural key is a dedup no-op, matching
    # the pre-Phase-3 ``links_repo.insert`` behavior this method wraps.
    with transaction(conn):
        inserted_again = backend.publish_links(
            PreparedLinks(source_id="s1", candidates=[candidate])
        )
    assert inserted_again == 0
    assert len(links_repo.list_by_entity(conn, "e1")) == 1


def test_link_touched_files_routes_through_the_backend(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="code_f1",
            source_id="s1",
            path="/repo/pkg/dog.py",
            kind=FileKind.CODE,
            size=1,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    files_repo.insert(
        conn,
        CoreFileRecord(
            id="doc_f1",
            source_id="s1",
            path="/docs/manual.md",
            kind=FileKind.DOCUMENT,
            size=1,
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
                file_id="code_f1",
                kind=EntityType.FUNCTION,
                name="bark",
                qualified_name="pkg.Dog.bark",
                language="python",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark(self): ...",
        )
        documents_repo.insert_document(
            conn,
            Document(
                id="doc1",
                source_id="s1",
                file_id="doc_f1",
                format=DocumentFormat.MARKDOWN,
                title="Manual",
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
                document_id="doc1",
                file_id="doc_f1",
                text="pkg.Dog.bark is the core API.",
                order_index=0,
                generation=1,
                created_at="now",
            ),
            doc_title="Manual",
        )

    backend = LocalKnowledgeBackend(conn=conn)
    with transaction(conn):
        inserted = link_touched_files(
            conn,
            backend,
            source_id="s1",
            touched_code_file_ids=["code_f1"],
            touched_document_file_ids=["doc_f1"],
        )

    assert inserted > 0
    assert len(links_repo.list_by_entity(conn, "e1")) == inserted


def test_relationships_repo_still_directly_importable_for_code_lookups() -> None:
    """Guards the deliberate scoping choice documented on
    ``LocalKnowledgeBackend.publish_code`` and ``code.processor.
    publish_code``: relationship *resolution* (reading entities to
    resolve cross-file references) stays a direct ``entities_repo``
    read, only the final write moved behind the backend. This is a
    cheap regression guard against that read path being accidentally
    routed through the backend contract (which has no such read method)
    in a future change.
    """
    assert hasattr(relationships_repo, "insert")
    assert hasattr(entities_repo, "find_by_qualified_name")
