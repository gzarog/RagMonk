"""Storage backend abstraction plan, Phase 5: integration tests for the
Elasticsearch ``KnowledgeBackend`` adapter against a REAL cluster.

Every test here is marked ``@pytest.mark.elasticsearch_integration`` (see
``pyproject.toml``'s ``markers``/``addopts`` -- excluded from the default
run, same convention as ``opensearch_integration``) and additionally
guarded by a module-level ``pytest.importorskip("elasticsearch")`` plus a
live-reachability check against ``ELASTICSEARCH_URL``: with no
``elasticsearch`` package installed, or no reachable cluster at that URL,
every test in this module SKIPS (not fails, not errors).

Run for real with, e.g.:

    ELASTICSEARCH_URL=https://localhost:9200 \\
        pytest -m elasticsearch_integration tests/unit/test_backends_elasticsearch_integration.py

These were authored and reviewed for correctness against the
``elasticsearch`` 8.x Python client's documented API, but NOT executed
against a real cluster in this environment (no Docker/live Elasticsearch
available here) -- see the Phase 5 PR comment/report for that caveat.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

elasticsearch = pytest.importorskip("elasticsearch")

from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend  # noqa: E402
from ragmonk.backends.elasticsearch_client import ElasticsearchConnectionError  # noqa: E402
from ragmonk.backends.models import (  # noqa: E402
    FileRecord,
    PreparedCode,
)
from ragmonk.core.config import ServerStorageConfig  # noqa: E402
from ragmonk.core.models import Entity, EntityType  # noqa: E402

_ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "")


def _cluster_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        client = elasticsearch.Elasticsearch(hosts=[url], verify_certs=False, request_timeout=2)
        client.info()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.elasticsearch_integration,
    pytest.mark.skipif(
        not _cluster_reachable(_ELASTICSEARCH_URL),
        reason=(
            "no reachable Elasticsearch cluster at $ELASTICSEARCH_URL "
            f"({_ELASTICSEARCH_URL or '<unset>'}) -- set it to a real cluster to run these"
        ),
    ),
]


@pytest.fixture
def backend() -> Iterator[ElasticsearchKnowledgeBackend]:
    prefix = f"ragmonk-it-{uuid.uuid4().hex[:8]}"
    config = ServerStorageConfig(
        engine="elasticsearch", url=_ELASTICSEARCH_URL, index_prefix=prefix
    )
    instance = ElasticsearchKnowledgeBackend(config)
    instance.ensure_schema()
    try:
        yield instance
    finally:
        try:
            for index in (
                f"{prefix}-files",
                f"{prefix}-content",
                f"{prefix}-relationships",
            ):
                instance._get_client().indices.delete(  # noqa: SLF001
                    index=index, ignore_unavailable=True
                )
        finally:
            instance.close()


def test_ensure_schema_creates_indices(backend: ElasticsearchKnowledgeBackend) -> None:
    assert backend.health() is True


def test_bulk_indexing_round_trip(backend: ElasticsearchKnowledgeBackend) -> None:
    # Mirror the real indexing lifecycle (begin -> publish_code ->
    # publish_generation): reads only see a source's *published*
    # generation, so unactivated writes are correctly invisible.
    gen = backend.begin_generation("s1")
    entity = Entity(
        id="e1",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name="do_thing",
        qualified_name="mod.do_thing",
        language="python",
        start_line=1,
        end_line=5,
        generation=int(gen),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=int(gen), entities=[entity])
    )
    backend.publish_generation("s1", gen)
    entities = backend.get_entities_for_files(["f1"])
    assert len(entities) == 1
    assert entities[0]["entity_id"] == "e1"


def test_lexical_search_returns_expected_hits(backend: ElasticsearchKnowledgeBackend) -> None:
    # Mirror the real indexing lifecycle (begin -> publish_code ->
    # publish_generation): reads only see a source's *published*
    # generation, so unactivated writes are correctly invisible.
    gen = backend.begin_generation("s1")
    entity = Entity(
        id="e1",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name="frobnicate_widget",
        qualified_name="mod.frobnicate_widget",
        language="python",
        start_line=1,
        end_line=5,
        generation=int(gen),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    backend.publish_code(
        PreparedCode(
            file_id="f1",
            source_id="s1",
            generation=int(gen),
            entities=[entity],
            entity_snippets={"e1": "def frobnicate_widget(): pass"},
        )
    )
    backend.publish_generation("s1", gen)
    hits = backend.lexical_search("frobnicate_widget", limit=5)
    assert any(h.payload.get("entity_id") == "e1" for h in hits)


def test_semantic_search_knn_round_trip(backend: ElasticsearchKnowledgeBackend) -> None:
    # Mirror the real indexing lifecycle (begin -> publish_code ->
    # publish_generation): reads only see a source's *published*
    # generation, so unactivated writes are correctly invisible.
    from ragmonk.backends.models import PreparedEmbeddings
    from ragmonk.core.models import EmbeddingSubjectType

    gen = backend.begin_generation("s1")

    entity = Entity(
        id="e1",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name="vectorized_thing",
        qualified_name="mod.vectorized_thing",
        language="python",
        start_line=1,
        end_line=5,
        generation=int(gen),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=int(gen), entities=[entity])
    )
    backend.publish_embeddings(
        PreparedEmbeddings(
            source_id="s1",
            subjects=[(EmbeddingSubjectType.ENTITY, "e1", "f1", "text")],
            vectors=[[0.1, 0.2, 0.3, 0.4]],
        )
    )
    backend.publish_generation("s1", gen)
    hits = backend.semantic_search([0.1, 0.2, 0.3, 0.4], limit=5)
    assert any(h.payload.get("entity_id") == "e1" for h in hits)


def test_incremental_upsert_and_delete(backend: ElasticsearchKnowledgeBackend) -> None:
    record = FileRecord(file_id="f1", source_id="s1", path="a.py", content_hash="hash1")
    backend.upsert_file(record)
    assert backend.get_file("f1") is not None

    updated = FileRecord(file_id="f1", source_id="s1", path="a.py", content_hash="hash2")
    backend.upsert_file(updated)
    fetched = backend.get_file("f1")
    assert fetched is not None
    assert fetched.content_hash == "hash2"

    backend.delete_file("s1", "f1")
    assert backend.get_file("f1") is None


def test_rebuild_generation_atomic_publish(backend: ElasticsearchKnowledgeBackend) -> None:
    gen = backend.begin_generation("s1")
    entity = Entity(
        id="e1",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name="x",
        qualified_name="mod.x",
        language="python",
        start_line=1,
        end_line=2,
        generation=int(gen),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    backend.publish_code(
        PreparedCode(file_id="f1", source_id="s1", generation=int(gen), entities=[entity])
    )
    backend.publish_generation("s1", gen)
    assert backend.get_entities_for_files(["f1"])


def test_server_outage_raises_no_local_fallback() -> None:
    """A cluster URL that refuses connections must raise a clear error,
    never silently fall back to any local/empty result.
    """
    config = ServerStorageConfig(
        engine="elasticsearch", url="http://127.0.0.1:1", request_timeout_seconds=1.0
    )
    backend = ElasticsearchKnowledgeBackend(config)
    with pytest.raises(ElasticsearchConnectionError):
        backend.describe()
