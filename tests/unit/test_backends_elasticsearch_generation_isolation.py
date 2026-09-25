"""Storage backend abstraction plan follow-up: proves the Elasticsearch
adapter's read methods (``lexical_search``/``semantic_search``/
``symbol_search``/``graph_neighbors``/``get_entities_for_files``/
``get_document_units_for_files``/``count_stats``) never return a
generation's documents until ``publish_generation`` has made that
generation active, and never return them again once
``abort_generation`` has discarded them -- the correctness gap this
adds a fix for (see ``elasticsearch.py``'s ``_generation_filter_clause``).

Mirrors ``test_backends_opensearch_generation_isolation.py`` scenario for
scenario, against the separate Elasticsearch adapter/fake.
"""

from __future__ import annotations

from tests.unit._fake_elasticsearch import FakeElasticsearch

from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend
from ragmonk.backends.models import (
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
)
from ragmonk.core.config import ServerStorageConfig
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


def _config() -> ServerStorageConfig:
    return ServerStorageConfig(engine="elasticsearch", url="http://fake:9200")


def _backend(fake: FakeElasticsearch) -> ElasticsearchKnowledgeBackend:
    return ElasticsearchKnowledgeBackend(_config(), client=fake)


def _entity(entity_id: str, name: str, file_id: str, generation: int) -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=f"mod.{name}",
        language="python",
        start_line=1,
        end_line=5,
        generation=generation,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


def _relationship(
    rel_id: str, source_entity_id: str, target_entity_id: str, file_id: str
) -> CoreRelationship:
    return CoreRelationship(
        id=rel_id,
        relationship_type=RelationshipType.CALLS,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        resolver="ast",
        confidence=Confidence.EXACT,
        file_id=file_id,
        generation=0,
        created_at="2024-01-01T00:00:00Z",
    )


def test_first_ever_generation_invisible_until_published() -> None:
    """(d): a source with no prior generation still establishes a
    sensible initial active generation, and that generation's writes are
    invisible until published -- matching P4/P5's original
    begin/publish lifecycle design (the default active generation for an
    unmarked source is ``"0"``, and ``begin_generation`` returns ``"1"``
    for it).
    """
    fake = FakeElasticsearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    assert gen1 == "1"
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity("e1", "first_fn", "f1", int(gen1))],
        )
    )
    # Not yet published: invisible to every generation-filtered read.
    assert backend.symbol_search("first_fn") == []
    assert backend.get_entities_for_files(["f1"]) == []
    assert backend.count_stats().entities == 0

    backend.publish_generation("s1", gen1)

    hits = backend.symbol_search("first_fn")
    assert any(h.payload.get("entity_id") == "e1" for h in hits)
    assert len(backend.get_entities_for_files(["f1"])) == 1
    assert backend.count_stats().entities == 1


def test_mid_rebuild_writes_invisible_to_every_read_method() -> None:
    """(a): once generation 1 is published, a second, in-progress
    generation's writes (entities, relationships, chunks, embeddings)
    must never leak into any read method's results -- only generation
    1's data comes back.
    """
    fake = FakeElasticsearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity("e-old", "old_fn", "f1", int(gen1))],
            relationships=[_relationship("r-old", "e-old", "e-old-target", "f1")],
        )
    )
    backend.publish_document(
        PreparedDocument(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            document=Document(
                id="doc-old",
                source_id="s1",
                file_id="f1",
                format=DocumentFormat.MARKDOWN,
                generation=int(gen1),
                created_at="2024-01-01T00:00:00Z",
                updated_at="2024-01-01T00:00:00Z",
            ),
            chunk_ids=["c-old"],
            chunks=[
                Chunk(
                    kind="paragraph",
                    text="old content",
                    heading_level=None,
                    heading_path=(),
                    parent_index=None,
                    page_start=None,
                    page_end=None,
                    search_text="old content",
                )
            ],
            doc_title="Old Doc",
        )
    )
    backend.publish_embeddings(
        PreparedEmbeddings(
            source_id="s1",
            subjects=[(EmbeddingSubjectType.ENTITY, "e-old", "f1", "text")],
            vectors=[[1.0, 0.0, 0.0]],
        )
    )
    backend.publish_generation("s1", gen1)

    # A second, in-progress generation -- never published in this test.
    gen2 = backend.begin_generation("s1")
    assert gen2 != gen1
    backend.publish_code(
        PreparedCode(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            entities=[_entity("e-new", "new_fn", "f2", int(gen2))],
            relationships=[_relationship("r-new", "e-old", "e-new-target", "f2")],
        )
    )
    backend.publish_document(
        PreparedDocument(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            document=Document(
                id="doc-new",
                source_id="s1",
                file_id="f2",
                format=DocumentFormat.MARKDOWN,
                generation=int(gen2),
                created_at="2024-01-01T00:00:00Z",
                updated_at="2024-01-01T00:00:00Z",
            ),
            chunk_ids=["c-new"],
            chunks=[
                Chunk(
                    kind="paragraph",
                    text="brand new content",
                    heading_level=None,
                    heading_path=(),
                    parent_index=None,
                    page_start=None,
                    page_end=None,
                    search_text="brand new content",
                )
            ],
            doc_title="New Doc",
        )
    )

    # lexical_search: only the published generation's chunk matches.
    lexical_hits = backend.lexical_search("content", limit=10)
    assert {h.payload.get("chunk_id") for h in lexical_hits} == {"c-old"}

    # semantic_search: the new generation wrote no embedding, but even a
    # trivially-matching kNN query must not surface entities from an
    # unpublished generation.
    semantic_hits = backend.semantic_search([1.0, 0.0, 0.0], limit=10)
    assert {h.payload.get("entity_id") for h in semantic_hits} == {"e-old"}

    # symbol_search: only the old generation's entity is visible.
    symbol_hits = backend.symbol_search("new_fn")
    assert symbol_hits == []
    symbol_hits_old = backend.symbol_search("old_fn")
    assert any(h.payload.get("entity_id") == "e-old" for h in symbol_hits_old)

    # graph_neighbors from e-old: only the published relationship (to
    # e-old-target) is reachable, never the in-progress one (to
    # e-new-target) even though it shares the same source_entity_id.
    neighbors = backend.graph_neighbors("e-old", "out", depth=1)
    neighbor_ids = {h.id for h in neighbors} | {
        h.payload.get("target_entity_id") for h in neighbors
    }
    assert "e-old-target" in {h.payload.get("target_entity_id") for h in neighbors}
    assert "e-new-target" not in neighbor_ids

    # get_entities_for_files / get_document_units_for_files.
    assert {e["entity_id"] for e in backend.get_entities_for_files(["f1", "f2"])} == {"e-old"}
    assert {u["chunk_id"] for u in backend.get_document_units_for_files(["f1", "f2"])} == {"c-old"}

    # count_stats reflects only the published generation.
    stats = backend.count_stats()
    assert stats.entities == 1
    assert stats.document_units == 1
    assert stats.embeddings == 1


def test_publish_generation_flips_visibility_to_new_generation() -> None:
    """(b): once the in-progress generation is published, its documents
    become visible and the stale, superseded generation's leftover
    documents (never explicitly deleted in this test) become invisible.
    """
    fake = FakeElasticsearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity("e-old", "old_fn", "f1", int(gen1))],
        )
    )
    backend.publish_generation("s1", gen1)

    gen2 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            entities=[_entity("e-new", "new_fn", "f2", int(gen2))],
        )
    )
    backend.publish_generation("s1", gen2)

    hits_new = backend.symbol_search("new_fn")
    assert any(h.payload.get("entity_id") == "e-new" for h in hits_new)
    hits_old = backend.symbol_search("old_fn")
    assert hits_old == []
    assert {e["entity_id"] for e in backend.get_entities_for_files(["f1", "f2"])} == {"e-new"}
    assert backend.count_stats().entities == 1


def test_abort_generation_documents_never_returned_by_any_read() -> None:
    """(c): an aborted generation's documents are deleted outright, so
    no read method -- filtered or not -- can ever return them, before or
    after the abort.
    """
    fake = FakeElasticsearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity("e-old", "old_fn", "f1", int(gen1))],
        )
    )
    backend.publish_generation("s1", gen1)

    gen2 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            entities=[_entity("e-abort", "abort_fn", "f2", int(gen2))],
        )
    )
    # Before the abort: already invisible via the generation filter
    # (gen1 is still active).
    assert backend.symbol_search("abort_fn") == []

    backend.abort_generation("s1", gen2)

    # After the abort: gone from the underlying store entirely, so every
    # read method -- filtered or not -- returns nothing for it.
    assert backend.symbol_search("abort_fn") == []
    assert backend.get_entities_for_files(["f2"]) == []
    assert "e-abort" not in {
        doc.get("entity_id") for doc in fake.store.get("ragmonk-content", {}).values()
    }
    # gen1's already-published data is untouched by the abort.
    assert any(h.payload.get("entity_id") == "e-old" for h in backend.symbol_search("old_fn"))


def test_clear_source_removes_every_generation_not_just_active() -> None:
    """``clear_source`` must NOT be generation-filtered: a full-source
    delete has to remove an in-progress, unpublished generation's
    documents too, not just the currently active one.
    """
    fake = FakeElasticsearch()
    backend = _backend(fake)

    gen1 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen1),
            entities=[_entity("e-old", "old_fn", "f1", int(gen1))],
        )
    )
    backend.publish_generation("s1", gen1)

    gen2 = backend.begin_generation("s1")
    backend.publish_code(
        PreparedCode(
            file_id="f2",
            source_id="s1",
            generation=int(gen2),
            entities=[_entity("e-inflight", "inflight_fn", "f2", int(gen2))],
        )
    )
    # Not published -- still in the store, just invisible to filtered reads.
    assert any(
        doc.get("entity_id") == "e-inflight" for doc in fake.store["ragmonk-content"].values()
    )

    backend.clear_source("s1")

    remaining = fake.store.get("ragmonk-content", {})
    assert not any(doc.get("source_id") == "s1" for doc in remaining.values())
