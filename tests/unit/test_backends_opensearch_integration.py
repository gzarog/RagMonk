"""Storage backend abstraction plan, Phase 4: integration tests for the
OpenSearch ``KnowledgeBackend`` adapter against a REAL cluster.

Every test here is marked ``@pytest.mark.opensearch_integration`` (see
``pyproject.toml``'s ``markers``/``addopts`` -- excluded from the default
run, same convention as ``docling_pdf``/``embedding_model``/etc.) and
additionally guarded by a module-level ``pytest.importorskip("opensearchpy")``
plus a live-reachability check against ``OPENSEARCH_URL``: with no
``opensearchpy`` installed, or no reachable cluster at that URL, every
test in this module SKIPS (not fails, not errors).

Run for real with, e.g.:

    OPENSEARCH_URL=https://localhost:9200 \\
        pytest -m opensearch_integration tests/unit/test_backends_opensearch_integration.py

These were authored and reviewed for correctness but NOT executed against
a real cluster in this environment (no Docker/live OpenSearch available
here) -- see the Phase 4 PR comment/report for that caveat.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

opensearchpy = pytest.importorskip("opensearchpy")

from ragmonk.backends.models import (  # noqa: E402
    FileRecord,
    PreparedCode,
)
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend  # noqa: E402
from ragmonk.backends.opensearch_client import OpenSearchConnectionError  # noqa: E402
from ragmonk.core.config import ServerStorageConfig  # noqa: E402
from ragmonk.core.models import Entity, EntityType  # noqa: E402

pytestmark = pytest.mark.opensearch_integration

_OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "")


def _cluster_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        client = opensearchpy.OpenSearch(hosts=[url], verify_certs=False, timeout=2)
        client.info()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.opensearch_integration,
    pytest.mark.skipif(
        not _cluster_reachable(_OPENSEARCH_URL),
        reason=(
            "no reachable OpenSearch cluster at $OPENSEARCH_URL "
            f"({_OPENSEARCH_URL or '<unset>'}) -- set it to a real cluster to run these"
        ),
    ),
]


@pytest.fixture
def backend() -> Iterator[OpenSearchKnowledgeBackend]:
    prefix = f"ragmonk-it-{uuid.uuid4().hex[:8]}"
    config = ServerStorageConfig(engine="opensearch", url=_OPENSEARCH_URL, index_prefix=prefix)
    instance = OpenSearchKnowledgeBackend(config)
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
                instance._get_client().indices.delete(index=index, ignore=[404])  # noqa: SLF001
        finally:
            instance.close()


def test_ensure_schema_creates_indices(backend: OpenSearchKnowledgeBackend) -> None:
    assert backend.health() is True


def test_bulk_indexing_round_trip(backend: OpenSearchKnowledgeBackend) -> None:
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


def test_lexical_search_returns_expected_hits(backend: OpenSearchKnowledgeBackend) -> None:
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


def test_incremental_upsert_and_delete(backend: OpenSearchKnowledgeBackend) -> None:
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


def test_rebuild_generation_atomic_publish(backend: OpenSearchKnowledgeBackend) -> None:
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
        engine="opensearch", url="http://127.0.0.1:1", request_timeout_seconds=1.0
    )
    backend = OpenSearchKnowledgeBackend(config)
    with pytest.raises(OpenSearchConnectionError):
        backend.describe()
