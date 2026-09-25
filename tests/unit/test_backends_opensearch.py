"""Storage backend abstraction plan, Phase 4: unit tests for the
OpenSearch ``KnowledgeBackend`` adapter, against ``FakeOpenSearch`` (an
in-memory double implementing just the ``opensearch-py`` client surface
this adapter calls -- see ``_fake_opensearch.py``). No real cluster, no
``opensearch-py`` import required for these tests: ``OpenSearchKnowledgeBackend``
accepts a pre-built ``client=`` override precisely so tests (and any
future caller with its own client) can bypass ``opensearch_client.build_client``.
"""

from __future__ import annotations

import pytest
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends import opensearch_ids as ids
from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    LinkCandidate,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
)
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.backends.opensearch_bulk import (
    BulkAction,
    BulkIndexError,
    run_bulk,
    run_bulk_or_raise,
)
from ragmonk.backends.opensearch_client import OpenSearchConnectionError
from ragmonk.core.config import BulkConfig, ServerStorageConfig
from ragmonk.core.models import (
    Confidence,
    Document,
    DocumentFormat,
    EmbeddingSubjectType,
    Entity,
    EntityType,
    RelationshipType,
)
from ragmonk.core.models import Relationship as CoreRelationship
from ragmonk.documents.chunker import Chunk


def _config(**overrides: object) -> ServerStorageConfig:
    return ServerStorageConfig(engine="opensearch", url="http://fake:9200", **overrides)  # type: ignore[arg-type]


def _backend(
    client: FakeOpenSearch | None = None, **overrides: object
) -> OpenSearchKnowledgeBackend:
    fake = client or FakeOpenSearch()
    backend = OpenSearchKnowledgeBackend(_config(**overrides), client=fake)
    return backend


def _entity(entity_id: str = "e1", file_id: str = "f1", source_id: str = "s1") -> Entity:
    return Entity(
        id=entity_id,
        source_id=source_id,
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name="do_thing",
        qualified_name="mod.do_thing",
        language="python",
        start_line=1,
        end_line=5,
        generation=1,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


def _relationship(rel_id: str = "r1", file_id: str = "f1") -> CoreRelationship:
    return CoreRelationship(
        id=rel_id,
        relationship_type=RelationshipType.CALLS,
        source_entity_id="e1",
        target_entity_id="e2",
        resolver="ast",
        confidence=Confidence.EXACT,
        file_id=file_id,
        generation=1,
        created_at="2024-01-01T00:00:00Z",
    )


# -- deterministic ids ----------------------------------------------------


def test_deterministic_ids_are_stable_across_calls() -> None:
    a = ids.entity_doc_id("s1", "f1", "e1")
    b = ids.entity_doc_id("s1", "f1", "e1")
    assert a == b
    assert a != ids.entity_doc_id("s1", "f1", "e2")
    assert a != ids.entity_doc_id("s2", "f1", "e1")


def test_deterministic_link_id_is_a_natural_key() -> None:
    a = ids.link_doc_id("s1", "e1", "d1", "sec1", "documented_by", "resolver-a")
    b = ids.link_doc_id("s1", "e1", "d1", "sec1", "documented_by", "resolver-a")
    assert a == b
    assert a != ids.link_doc_id("s1", "e1", "d1", "sec2", "documented_by", "resolver-a")


# -- health / no silent fallback ------------------------------------------


def test_health_true_when_reachable() -> None:
    backend = _backend(FakeOpenSearch(reachable=True))
    assert backend.health() is True


def test_health_false_when_unreachable() -> None:
    backend = _backend(FakeOpenSearch(reachable=False))
    assert backend.health() is False


def test_unreachable_cluster_raises_not_empty_results() -> None:
    """No silent local fallback: an unreachable OpenSearch backend must
    raise on a real operation, never quietly return an empty/local
    result that could be mistaken for "nothing found".
    """
    backend = _backend(FakeOpenSearch(reachable=False))
    with pytest.raises(OpenSearchConnectionError):
        backend.describe()


# -- schema -----------------------------------------------------------------


def test_ensure_schema_creates_three_indices_idempotently() -> None:
    fake = FakeOpenSearch()
    backend = _backend(fake)
    backend.ensure_schema()
    assert set(fake.store.keys()) == {"ragmonk-files", "ragmonk-content", "ragmonk-relationships"}
    # Second call is a no-op, not an error / not a re-create.
    backend.ensure_schema()
    assert set(fake.store.keys()) == {"ragmonk-files", "ragmonk-content", "ragmonk-relationships"}


# -- bulk batching / retries / failures --------------------------------------


def test_bulk_batches_by_max_actions() -> None:
    fake = FakeOpenSearch()
    config = BulkConfig(max_actions=2, max_bytes=10_000_000, concurrency=1, max_retries=0)
    actions = [
        BulkAction(op="index", index="idx", doc_id=f"d{i}", source={"n": i}) for i in range(5)
    ]
    result = run_bulk(fake, actions, config)
    assert result.succeeded == 5
    assert not result.failed
    # 5 actions batched by 2 => 3 bulk() calls.
    assert len(fake.bulk_calls) == 3


def test_bulk_retries_transient_failures_and_succeeds() -> None:
    fake = FakeOpenSearch(fail_ids={"d0": 2})
    config = BulkConfig(max_actions=10, max_bytes=10_000_000, concurrency=1, max_retries=3)
    actions = [BulkAction(op="index", index="idx", doc_id="d0", source={"n": 0})]
    result = run_bulk(fake, actions, config)
    assert result.succeeded == 1
    assert not result.failed
    assert fake.store["idx"]["d0"] == {"n": 0}


def test_bulk_surfaces_failure_once_retries_exhausted() -> None:
    fake = FakeOpenSearch(fail_ids={"d0": 100})
    config = BulkConfig(max_actions=10, max_bytes=10_000_000, concurrency=1, max_retries=2)
    actions = [BulkAction(op="index", index="idx", doc_id="d0", source={"n": 0})]
    result = run_bulk(fake, actions, config)
    assert result.succeeded == 0
    assert len(result.failed) == 1
    assert result.failed[0].doc_id == "d0"
    assert result.failed[0].status == 503


def test_run_bulk_or_raise_raises_bulk_index_error_with_detail() -> None:
    fake = FakeOpenSearch(fail_ids={"d0": 100})
    config = BulkConfig(max_actions=10, max_bytes=10_000_000, concurrency=1, max_retries=0)
    actions = [BulkAction(op="index", index="idx", doc_id="d0", source={"n": 0})]
    with pytest.raises(BulkIndexError) as excinfo:
        run_bulk_or_raise(fake, actions, config)
    assert "d0" in str(excinfo.value)
    assert excinfo.value.failures[0].doc_id == "d0"


def test_bulk_partial_failure_does_not_swallow_successes() -> None:
    fake = FakeOpenSearch(fail_ids={"bad": 100})
    config = BulkConfig(max_actions=10, max_bytes=10_000_000, concurrency=1, max_retries=0)
    actions = [
        BulkAction(op="index", index="idx", doc_id="good1", source={"n": 1}),
        BulkAction(op="index", index="idx", doc_id="bad", source={"n": 2}),
        BulkAction(op="index", index="idx", doc_id="good2", source={"n": 3}),
    ]
    result = run_bulk(fake, actions, config)
    assert result.succeeded == 2
    assert [f.doc_id for f in result.failed] == ["bad"]
    assert set(fake.store["idx"].keys()) == {"good1", "good2"}


# -- upsert/delete file -------------------------------------------------------


def test_upsert_file_then_get_file() -> None:
    backend = _backend()
    record = FileRecord(
        file_id="f1", source_id="s1", path="a/b.py", content_hash="hash1", size_bytes=10
    )
    backend.upsert_file(record)
    fetched = backend.get_file("f1")
    assert fetched is not None
    assert fetched.source_id == "s1"
    assert fetched.path == "a/b.py"
    assert fetched.content_hash == "hash1"


def test_delete_file_removes_file_and_scoped_docs() -> None:
    fake = FakeOpenSearch()
    backend = _backend(fake)
    backend.upsert_file(FileRecord(file_id="f1", source_id="s1", path="a.py", content_hash="h"))
    backend.publish_code(
        PreparedCode(
            file_id="f1", source_id="s1", generation=1, entities=[_entity()], relationships=[]
        )
    )
    backend.delete_file("s1", "f1")
    assert backend.get_file("f1") is None
    assert backend.get_entities_for_files(["f1"]) == []


# -- publish_code -------------------------------------------------------------


def test_publish_code_indexes_entities_and_relationships() -> None:
    backend = _backend()
    # generation=0: the default active generation for a source with no
    # marker doc yet -- these reads go through the generation-filtered
    # query path added for read-time generation isolation, so a write
    # tagged with a *non-default*, never-published generation would be
    # invisible to them (see the "generation lifecycle" tests below).
    prepared = PreparedCode(
        file_id="f1",
        source_id="s1",
        generation=0,
        entities=[_entity()],
        entity_snippets={"e1": "def do_thing(): ..."},
        relationships=[_relationship()],
    )
    backend.publish_code(prepared)

    entities = backend.get_entities_for_files(["f1"])
    assert len(entities) == 1
    assert entities[0]["entity_id"] == "e1"
    assert entities[0]["snippet"] == "def do_thing(): ..."

    hits = backend.symbol_search("do_thing")
    assert any(hit.payload.get("entity_id") == "e1" for hit in hits)

    neighbors = backend.graph_neighbors("e1", "out", depth=1)
    assert any(hit.payload.get("relationship_type") == "calls" for hit in neighbors)


def test_publish_code_clear_only_removes_previous_generation() -> None:
    backend = _backend()
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=0, entities=[_entity()])
    )
    assert backend.get_entities_for_files(["f1"]) != []
    backend.publish_code(PreparedCode(file_id="f1", source_id="s1", generation=1, clear_only=True))
    assert backend.get_entities_for_files(["f1"]) == []


def test_publish_code_reindex_is_idempotent_not_duplicating() -> None:
    """Re-publishing the same file's entities must overwrite via
    deterministic ids, never accumulate duplicates.
    """
    backend = _backend()
    prepared = PreparedCode(file_id="f1", source_id="s1", generation=0, entities=[_entity()])
    backend.publish_code(prepared)
    backend.publish_code(prepared)
    assert len(backend.get_entities_for_files(["f1"])) == 1


# -- publish_document -----------------------------------------------------


def test_publish_document_indexes_document_and_chunks() -> None:
    backend = _backend()
    document = Document(
        id="doc1",
        source_id="s1",
        file_id="f1",
        format=DocumentFormat.MARKDOWN,
        generation=0,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    chunk = Chunk(
        kind="paragraph",
        text="hello world",
        heading_level=None,
        heading_path=("Intro",),
        parent_index=None,
        page_start=None,
        page_end=None,
        search_text="hello world",
    )
    prepared = PreparedDocument(
        file_id="f1",
        source_id="s1",
        generation=0,
        document=document,
        chunk_ids=["c1"],
        chunks=[chunk],
        doc_title="My Doc",
    )
    backend.publish_document(prepared)

    units = backend.get_document_units_for_files(["f1"])
    assert len(units) == 1
    assert units[0]["content"] == "hello world"

    hits = backend.lexical_search("hello", limit=10)
    assert any(h.payload.get("chunk_id") == "c1" for h in hits)


def test_publish_document_delete_only_clears_previous_generation() -> None:
    backend = _backend()
    document = Document(
        id="doc1",
        source_id="s1",
        file_id="f1",
        format=next(iter(DocumentFormat)),
        generation=1,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    chunk = Chunk(
        kind="paragraph",
        text="hello",
        heading_level=None,
        heading_path=(),
        parent_index=None,
        page_start=None,
        page_end=None,
        search_text="hello",
    )
    backend.publish_document(
        PreparedDocument(
            file_id="f1",
            source_id="s1",
            generation=0,
            document=document,
            chunk_ids=["c1"],
            chunks=[chunk],
        )
    )
    assert backend.get_document_units_for_files(["f1"]) != []
    backend.publish_document(
        PreparedDocument(file_id="f1", source_id="s1", generation=1, delete_only=True)
    )
    assert backend.get_document_units_for_files(["f1"]) == []


# -- publish_embeddings -----------------------------------------------------


def test_publish_embeddings_updates_existing_entity_without_wiping_fields() -> None:
    backend = _backend()
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=0, entities=[_entity()])
    )
    embeddings = PreparedEmbeddings(
        source_id="s1",
        model_id="model-a",
        subjects=[(EmbeddingSubjectType.ENTITY, "e1", "f1", "text")],
        vectors=[[0.1, 0.2, 0.3]],
    )
    backend.publish_embeddings(embeddings)

    entities = backend.get_entities_for_files(["f1"])
    assert len(entities) == 1
    assert entities[0]["embedding"] == [0.1, 0.2, 0.3]
    # Original fields survive the partial update.
    assert entities[0]["name"] == "do_thing"


def test_publish_embeddings_noop_for_empty_subjects() -> None:
    backend = _backend()
    backend.publish_embeddings(PreparedEmbeddings(source_id="s1"))  # no raise


# -- publish_links ------------------------------------------------------------


def test_publish_links_returns_newly_inserted_count_and_dedupes() -> None:
    backend = _backend()
    candidate = LinkCandidate(
        entity_id="e1",
        document_id="d1",
        section_id=None,
        link_type=RelationshipType.DOCUMENTED_BY,
        resolver="resolver-a",
        confidence=Confidence.HIGH,
        evidence="matched name",
    )
    inserted_first = backend.publish_links(PreparedLinks(source_id="s1", candidates=[candidate]))
    assert inserted_first == 1
    inserted_second = backend.publish_links(PreparedLinks(source_id="s1", candidates=[candidate]))
    assert inserted_second == 0  # same natural key -> overwrite, not a new insert


def test_publish_links_empty_returns_zero() -> None:
    backend = _backend()
    assert backend.publish_links(PreparedLinks(source_id="s1", candidates=[])) == 0


# -- clear_source ---------------------------------------------------------


def test_clear_source_removes_all_three_indices_docs_for_that_source() -> None:
    fake = FakeOpenSearch()
    backend = _backend(fake)
    backend.upsert_file(FileRecord(file_id="f1", source_id="s1", path="a.py", content_hash="h"))
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=1,
            entities=[_entity()],
            relationships=[_relationship()],
        )
    )
    backend.publish_links(
        PreparedLinks(
            source_id="s1",
            candidates=[
                LinkCandidate(
                    entity_id="e1",
                    document_id="d1",
                    section_id=None,
                    link_type=RelationshipType.MENTIONED_IN,
                    resolver="r",
                    confidence=Confidence.MEDIUM,
                    evidence="e",
                )
            ],
        )
    )
    # Sanity: something is stored across all three indices before clearing.
    assert fake.store["ragmonk-files"]
    assert fake.store["ragmonk-content"]
    assert fake.store["ragmonk-relationships"]

    backend.clear_source("s1")

    for index_docs in fake.store.values():
        assert all(doc.get("source_id") != "s1" for doc in index_docs.values())
    assert backend.get_file("f1") is None
    assert backend.get_entities_for_files(["f1"]) == []


# -- generation lifecycle ----------------------------------------------------


def test_generation_publish_makes_docs_visible_and_abort_cleans_up() -> None:
    fake = FakeOpenSearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity(entity_id="e-gen1", file_id="f1")],
        )
    )
    backend.publish_generation("s1", gen1)

    # A *different* file's write, tagged with a new, not-yet-published
    # generation -- publish_code's own delete-then-insert only clears
    # *that file's* previous entities, so this is the clean way to prove
    # abort_generation leaves an unrelated, already-published file's
    # documents untouched.
    gen2 = backend.begin_generation("s1")
    assert gen2 != gen1
    backend.publish_code(
        PreparedCode(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            entities=[_entity(entity_id="e-gen2", file_id="f2")],
        )
    )
    # gen2's documents exist in the store even before publish (this
    # backend's marker-doc design doesn't hide unpublished writes from
    # direct id/file lookups -- only generation-scoped query methods; the
    # important invariant abort_generation protects is that aborting
    # gen2 never touches gen1's already-published documents).
    backend.abort_generation("s1", gen2)

    remaining_ids = {
        doc.get("entity_id")
        for doc in fake.store["ragmonk-content"].values()
        if doc.get("doc_kind") == "entity"
    }
    assert "e-gen1" in remaining_ids
    assert "e-gen2" not in remaining_ids


def test_begin_generation_increments_from_current_active() -> None:
    backend = _backend()
    first = backend.begin_generation("s1")
    assert first == "1"
    backend.publish_generation("s1", first)
    second = backend.begin_generation("s1")
    assert second == "2"


# -- count_stats --------------------------------------------------------------


def test_count_stats_reports_real_counts() -> None:
    backend = _backend()
    backend.upsert_file(FileRecord(file_id="f1", source_id="s1", path="a.py", content_hash="h"))
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=0, entities=[_entity()])
    )
    stats = backend.count_stats()
    assert isinstance(stats, BackendStats)
    assert stats.files == 1
    assert stats.entities == 1
