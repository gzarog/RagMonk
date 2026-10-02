"""Server Indexing Performance V3 completion: backend-level regression
coverage run against BOTH server adapters (OpenSearch and Elasticsearch)
through their in-memory fakes, so every assertion here doubles as an
OpenSearch/Elasticsearch parity check.

Covers:
- P3: a partially successful fresh-generation bulk (code and documents)
  is cleaned up before the error propagates, so a retry yields exactly one
  logical copy of each artifact; the published generation is untouched and
  incremental passes keep their existing behavior.
- P4: ``delete_files_batch`` groups whole-file removals into bounded
  ``terms`` deletes, removes every artifact (including cross-domain links
  touching a deleted file), stays source-scoped, and counts its requests.
- P5: fresh-generation link publication performs zero existence lookups,
  in-memory dedupe keeps the inserted count exact, and incremental
  publication still uses one batched lookup (never ``client.exists``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from tests.unit._fake_elasticsearch import FakeElasticsearch
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends import elasticsearch_ids, elasticsearch_mappings, opensearch_ids
from ragmonk.backends import opensearch_mappings as os_mappings
from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend
from ragmonk.backends.models import (
    FileRecord,
    LinkCandidate,
    PreparedCode,
    PreparedDocument,
    PreparedLinks,
    ServerWritePass,
)
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.core.config import BulkConfig, ServerStorageConfig
from ragmonk.core.models import (
    Confidence,
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    RelationshipType,
)
from ragmonk.core.models import Relationship as CoreRelationship
from ragmonk.documents.chunker import Chunk

_NO_RETRY_BULK = BulkConfig(max_actions=500, max_bytes=5_000_000, concurrency=1, max_retries=0)


class _Engine:
    """Bundles one engine's fake client, backend and id/mapping helpers."""

    def __init__(self, name: str, *, bulk: BulkConfig | None = None) -> None:
        self.name = name
        overrides: dict[str, Any] = {"bulk": bulk} if bulk is not None else {}
        config = ServerStorageConfig(engine=name, url="http://fake:9200", **overrides)  # type: ignore[arg-type]
        self.fake: Any
        self.backend: Any
        if name == "opensearch":
            self.fake = FakeOpenSearch()
            self.backend = OpenSearchKnowledgeBackend(config, client=self.fake)
            self.ids: Any = opensearch_ids
            self.mappings: Any = os_mappings
        else:
            self.fake = FakeElasticsearch()
            self.backend = ElasticsearchKnowledgeBackend(config, client=self.fake)
            self.ids = elasticsearch_ids
            self.mappings = elasticsearch_mappings
        self.prefix = self.backend._prefix

    @property
    def content_index(self) -> str:
        return str(self.mappings.content_index(self.prefix))

    @property
    def relationships_index(self) -> str:
        return str(self.mappings.relationships_index(self.prefix))

    @property
    def files_index(self) -> str:
        return str(self.mappings.files_index(self.prefix))

    def docs(self, index: str) -> dict[str, dict[str, Any]]:
        return dict(self.fake.store.get(index, {}))

    def count_search_calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Wraps the fake's ``search`` to count calls per index."""
        counts: dict[str, int] = {}
        original: Callable[..., Any] = self.fake.search

        def counted(*args: Any, **kwargs: Any) -> Any:
            index = kwargs.get("index", args[0] if args else "")
            counts[index] = counts.get(index, 0) + 1
            return original(*args, **kwargs)

        monkeypatch.setattr(self.fake, "search", counted)
        return counts


ENGINES = ["opensearch", "elasticsearch"]


def _entity(entity_id: str, file_id: str, *, source_id: str = "s1", generation: int = 1) -> Entity:
    return Entity(
        id=entity_id,
        source_id=source_id,
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=f"fn_{entity_id}",
        qualified_name=f"mod.fn_{entity_id}",
        language="python",
        start_line=1,
        end_line=2,
        generation=generation,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


def _relationship(rel_id: str, file_id: str, source_entity_id: str) -> CoreRelationship:
    return CoreRelationship(
        id=rel_id,
        relationship_type=RelationshipType.CALLS,
        source_entity_id=source_entity_id,
        target_entity_id=None,
        target_symbol="other",
        resolver="ast",
        confidence=Confidence.EXACT,
        file_id=file_id,
        generation=1,
        created_at="2024-01-01T00:00:00Z",
    )


def _code(file_id: str, entity_id: str, *, generation: int, source_id: str = "s1") -> PreparedCode:
    return PreparedCode(
        file_id=file_id,
        source_id=source_id,
        generation=generation,
        entities=[_entity(entity_id, file_id, source_id=source_id, generation=generation)],
        relationships=[_relationship(f"r-{entity_id}", file_id, entity_id)],
    )


def _document(
    file_id: str, document_id: str, chunk_ids: list[str], *, generation: int, source_id: str = "s1"
) -> PreparedDocument:
    document = Document(
        id=document_id,
        source_id=source_id,
        file_id=file_id,
        format=DocumentFormat.MARKDOWN,
        title=f"Title {document_id}",
        generation=generation,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    chunks = [
        Chunk(
            kind="paragraph",
            text=f"text {cid}",
            heading_level=None,
            heading_path=(),
            parent_index=None,
            page_start=None,
            page_end=None,
            search_text=f"text {cid}",
        )
        for cid in chunk_ids
    ]
    return PreparedDocument(
        file_id=file_id,
        source_id=source_id,
        generation=generation,
        document=document,
        chunk_ids=list(chunk_ids),
        chunks=chunks,
        doc_title=f"Title {document_id}",
    )


def _content_by(engine: _Engine, doc_kind: str, generation: str) -> list[dict[str, Any]]:
    return [
        src
        for src in engine.docs(engine.content_index).values()
        if src.get("doc_kind") == doc_kind and src.get("generation") == generation
    ]


# -- P3: fresh-generation partial bulk cleanup ------------------------------


@pytest.mark.parametrize("engine_name", ENGINES)
def test_fresh_generation_code_partial_bulk_is_cleaned_before_retry(engine_name: str) -> None:
    engine = _Engine(engine_name, bulk=_NO_RETRY_BULK)
    backend = engine.backend
    # The currently published generation (0) already holds f1's old entity.
    backend.publish_code(_code("f1", "old-e1", generation=0))

    pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=True)
    first_attempt = [_code("f1", "e1", generation=1), _code("f2", "e2", generation=1)]
    # f2's entity fails terminally; f1's entity/relationships are accepted.
    engine.fake.fail_ids[engine.ids.entity_doc_id("s1", "f2", "e2")] = 100
    refreshes_before = len(engine.fake.indices.refresh_calls)

    with pytest.raises(Exception):  # noqa: B017 - engine-specific BulkIndexError
        backend.publish_code_batch(first_attempt, server_write_pass=pass_ctx)

    # The partially accepted fresh-generation artifacts are gone ...
    assert _content_by(engine, "entity", "1") == []
    assert [
        s for s in engine.docs(engine.relationships_index).values() if s.get("generation") == "1"
    ] == []
    # ... after a refresh made them visible to delete-by-query, and the
    # cleanup requests are counted on the pass.
    assert len(engine.fake.indices.refresh_calls) > refreshes_before
    assert pass_ctx.delete_by_query_count == 3
    # The published generation is untouched.
    assert [e["entity_id"] for e in _content_by(engine, "entity", "0")] == ["old-e1"]

    # Retry: new UUID-style entity ids, no injected failure.
    engine.fake.fail_ids.clear()
    retry = [_code("f1", "e1-retry", generation=1), _code("f2", "e2-retry", generation=1)]
    backend.publish_code_batch(retry, server_write_pass=pass_ctx)

    fresh = sorted(e["entity_id"] for e in _content_by(engine, "entity", "1"))
    assert fresh == ["e1-retry", "e2-retry"]
    fresh_rels = sorted(
        s["relationship_id"]
        for s in engine.docs(engine.relationships_index).values()
        if s.get("doc_kind") == "relationship" and s.get("generation") == "1"
    )
    assert fresh_rels == ["r-e1-retry", "r-e2-retry"]
    assert [e["entity_id"] for e in _content_by(engine, "entity", "0")] == ["old-e1"]


@pytest.mark.parametrize("engine_name", ENGINES)
def test_fresh_generation_document_partial_bulk_is_cleaned_before_retry(
    engine_name: str,
) -> None:
    engine = _Engine(engine_name, bulk=_NO_RETRY_BULK)
    backend = engine.backend
    backend.publish_document(_document("f1", "old-doc", ["old-c1"], generation=0))

    pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=True)
    first_attempt = [
        _document("f1", "d1", ["c1a", "c1b"], generation=1),
        _document("f2", "d2", ["c2a"], generation=1),
    ]
    engine.fake.fail_ids[engine.ids.chunk_doc_id("s1", "f2", "c2a")] = 100

    with pytest.raises(Exception):  # noqa: B017 - engine-specific BulkIndexError
        backend.publish_document_batch(first_attempt, server_write_pass=pass_ctx)

    assert _content_by(engine, "document", "1") == []
    assert _content_by(engine, "chunk", "1") == []
    assert pass_ctx.delete_by_query_count == 3
    assert [c["chunk_id"] for c in _content_by(engine, "chunk", "0")] == ["old-c1"]

    engine.fake.fail_ids.clear()
    retry = [
        _document("f1", "d1-retry", ["c1a-r", "c1b-r"], generation=1),
        _document("f2", "d2-retry", ["c2a-r"], generation=1),
    ]
    backend.publish_document_batch(retry, server_write_pass=pass_ctx)

    assert sorted(d["document_id"] for d in _content_by(engine, "document", "1")) == [
        "d1-retry",
        "d2-retry",
    ]
    assert sorted(c["chunk_id"] for c in _content_by(engine, "chunk", "1")) == [
        "c1a-r",
        "c1b-r",
        "c2a-r",
    ]
    assert [d["document_id"] for d in _content_by(engine, "document", "0")] == ["old-doc"]


@pytest.mark.parametrize("engine_name", ENGINES)
def test_incremental_batch_failure_keeps_existing_behavior(engine_name: str) -> None:
    """Incremental passes already replace a file's artifacts on retry via
    the grouped replacement delete, so the fresh-generation cleanup must
    not run for them (no extra refresh/delete requests).
    """
    engine = _Engine(engine_name, bulk=_NO_RETRY_BULK)
    pass_ctx = ServerWritePass(source_id="s1", generation=0, generation_is_empty=False)
    engine.fake.fail_ids[engine.ids.entity_doc_id("s1", "f2", "e2")] = 100
    refreshes_before = len(engine.fake.indices.refresh_calls)

    with pytest.raises(Exception):  # noqa: B017 - engine-specific BulkIndexError
        engine.backend.publish_code_batch(
            [_code("f1", "e1", generation=0), _code("f2", "e2", generation=0)],
            server_write_pass=pass_ctx,
        )

    # Only the normal grouped replacement delete ran (3 requests), no refresh.
    assert pass_ctx.delete_by_query_count == 3
    assert len(engine.fake.indices.refresh_calls) == refreshes_before


# -- P4: grouped whole-file deletion ----------------------------------------


def _seed_source(engine: _Engine, source_id: str, file_ids: list[str]) -> None:
    for fid in file_ids:
        engine.backend.upsert_file(
            FileRecord(file_id=fid, source_id=source_id, path=f"{fid}.py", content_hash="h")
        )
        engine.backend.publish_code(
            _code(fid, f"{source_id}-e-{fid}", generation=0, source_id=source_id)
        )
        engine.backend.publish_document(
            _document(
                fid,
                f"{source_id}-d-{fid}",
                [f"{source_id}-c-{fid}"],
                generation=0,
                source_id=source_id,
            )
        )


def _artifacts_for(engine: _Engine, source_id: str, file_id: str) -> list[dict[str, Any]]:
    out = []
    for index in (engine.files_index, engine.content_index, engine.relationships_index):
        for src in engine.docs(index).values():
            if src.get("source_id") != source_id:
                continue
            if file_id in (
                src.get("file_id"),
                src.get("entity_file_id"),
                src.get("document_file_id"),
            ):
                out.append(src)
    return out


def _link(entity_id: str, document_id: str) -> LinkCandidate:
    return LinkCandidate(
        entity_id=entity_id,
        document_id=document_id,
        section_id=None,
        link_type=RelationshipType.DOCUMENTED_BY,
        resolver="name",
        confidence=Confidence.HIGH,
        evidence="matched",
    )


@pytest.mark.parametrize("engine_name", ENGINES)
def test_grouped_whole_file_deletion_removes_all_artifacts(engine_name: str) -> None:
    engine = _Engine(engine_name)
    file_ids = [f"f{i}" for i in range(5)]
    _seed_source(engine, "s1", file_ids)
    # Cross-domain links: f0's entity -> f4's document (touches a deleted
    # file on the entity side), f3's entity -> f1's document (touches a
    # deleted file on the document side), f3's entity -> f4's document
    # (touches no deleted file and must survive).
    engine.backend.publish_links(
        PreparedLinks(
            source_id="s1",
            candidates=[
                _link("s1-e-f0", "s1-d-f4"),
                _link("s1-e-f3", "s1-d-f1"),
                _link("s1-e-f3", "s1-d-f4"),
            ],
        )
    )
    links_before = [
        s for s in engine.docs(engine.relationships_index).values() if s["doc_kind"] == "link"
    ]
    assert len(links_before) == 3

    pass_ctx = ServerWritePass(source_id="s1", generation=0, generation_is_empty=False)
    calls_before = len(engine.fake.delete_by_query_calls)
    engine.backend.delete_files_batch("s1", ["f0", "f1", "f2"], server_write_pass=pass_ctx)

    # One grouped terms delete per index plus one grouped link delete --
    # NOT three full per-file delete sequences.
    issued = len(engine.fake.delete_by_query_calls) - calls_before
    assert issued == len(engine.mappings.all_indices(engine.prefix)) + 1
    assert pass_ctx.delete_by_query_count == issued
    for fid in ("f0", "f1", "f2"):
        assert _artifacts_for(engine, "s1", fid) == []
    for fid in ("f3", "f4"):
        kinds = {src["doc_kind"] for src in _artifacts_for(engine, "s1", fid)}
        assert {"file", "entity", "relationship", "document", "chunk"} <= kinds
    remaining_links = [
        (s["entity_id"], s["document_id"])
        for s in engine.docs(engine.relationships_index).values()
        if s["doc_kind"] == "link"
    ]
    assert remaining_links == [("s1-e-f3", "s1-d-f4")]


@pytest.mark.parametrize("engine_name", ENGINES)
def test_grouped_whole_file_deletion_is_source_scoped(engine_name: str) -> None:
    engine = _Engine(engine_name)
    # Same file ids in two different sources.
    _seed_source(engine, "s1", ["f0", "f1"])
    _seed_source(engine, "s2", ["f0", "f1"])

    engine.backend.delete_files_batch(
        "s1",
        ["f0", "f1"],
        server_write_pass=ServerWritePass(source_id="s1", generation=0, generation_is_empty=False),
    )

    for fid in ("f0", "f1"):
        assert _artifacts_for(engine, "s1", fid) == []
        assert _artifacts_for(engine, "s2", fid) != []


@pytest.mark.parametrize("engine_name", ENGINES)
def test_grouped_whole_file_deletion_bounds_terms_batches(
    engine_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ragmonk.backends.server_common as common

    monkeypatch.setattr(common, "_TERMS_BATCH", 2)
    engine = _Engine(engine_name)
    _seed_source(engine, "s1", [f"f{i}" for i in range(5)])

    calls_before = len(engine.fake.delete_by_query_calls)
    engine.backend.delete_files_batch("s1", [f"f{i}" for i in range(5)])

    per_group = len(engine.mappings.all_indices(engine.prefix)) + 1
    assert len(engine.fake.delete_by_query_calls) - calls_before == 3 * per_group
    for i in range(5):
        assert _artifacts_for(engine, "s1", f"f{i}") == []


@pytest.mark.parametrize("engine_name", ENGINES)
def test_grouped_whole_file_deletion_empty_is_noop(engine_name: str) -> None:
    engine = _Engine(engine_name)
    engine.backend.delete_files_batch("s1", [])
    assert engine.fake.delete_by_query_calls == []


def test_default_delete_files_batch_loops_delete_file() -> None:
    """The base-contract default keeps non-server backends compatible."""
    backend = MagicMock(spec=KnowledgeBackend)
    KnowledgeBackend.delete_files_batch(backend, "s1", ["a", "b"])
    assert [c.args for c in backend.delete_file.call_args_list] == [("s1", "a"), ("s1", "b")]


# -- P5: link publication ---------------------------------------------------


def _seed_link_ends(engine: _Engine, generation: int) -> None:
    for i in range(3):
        engine.backend.publish_code(_code(f"cf{i}", f"e{i}", generation=generation))
        engine.backend.publish_document(
            _document(f"df{i}", f"d{i}", [f"c{i}"], generation=generation)
        )


@pytest.mark.parametrize("engine_name", ENGINES)
def test_fresh_generation_link_publish_performs_zero_existence_lookups(
    engine_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(engine_name)
    _seed_link_ends(engine, generation=1)
    counts = engine.count_search_calls(monkeypatch)
    pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=True)

    inserted = engine.backend.publish_links(
        PreparedLinks(
            source_id="s1",
            generation=1,
            candidates=[_link("e0", "d0"), _link("e1", "d1"), _link("e0", "d0")],
        ),
        server_write_pass=pass_ctx,
    )

    assert inserted == 2
    assert counts.get(engine.relationships_index, 0) == 0
    assert engine.fake.exists_calls == 0
    assert pass_ctx.bulk_actions == 2


@pytest.mark.parametrize("engine_name", ENGINES)
def test_link_dedupe_preserves_inserted_count_incrementally(
    engine_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(engine_name)
    _seed_link_ends(engine, generation=0)
    # e0->d0 already exists in the active generation.
    assert (
        engine.backend.publish_links(PreparedLinks(source_id="s1", candidates=[_link("e0", "d0")]))
        == 1
    )

    counts = engine.count_search_calls(monkeypatch)
    pass_ctx = ServerWritePass(source_id="s1", generation=0, generation_is_empty=False)
    bulk_before = len(engine.fake.bulk_calls)
    inserted = engine.backend.publish_links(
        PreparedLinks(
            source_id="s1",
            candidates=[
                _link("e0", "d0"),
                _link("e1", "d1"),
                _link("e1", "d1"),
                _link("e2", "d2"),
                _link("e2", "d2"),
            ],
        ),
        server_write_pass=pass_ctx,
    )

    # Two genuinely new links; duplicates collapse to one action each.
    assert inserted == 2
    assert pass_ctx.bulk_actions == 3
    new_bulk_calls = engine.fake.bulk_calls[bulk_before:]
    assert sum(len(call) for call in new_bulk_calls) == 3 * 2  # meta + source lines
    # Incremental publication keeps exactly one batched lookup, no exists().
    assert counts.get(engine.relationships_index, 0) == 1
    assert engine.fake.exists_calls == 0

    # Idempotent republish: nothing new.
    assert (
        engine.backend.publish_links(
            PreparedLinks(source_id="s1", candidates=[_link("e1", "d1"), _link("e2", "d2")]),
            server_write_pass=pass_ctx,
        )
        == 0
    )


@pytest.mark.parametrize("engine_name", ENGINES)
def test_incremental_links_use_single_batched_lookup_for_many_candidates(
    engine_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(engine_name)
    counts = engine.count_search_calls(monkeypatch)
    candidates = [_link(f"x{i}", f"y{i}") for i in range(40)]
    pass_ctx = ServerWritePass(source_id="s1", generation=0, generation_is_empty=False)
    assert (
        engine.backend.publish_links(
            PreparedLinks(source_id="s1", candidates=candidates), server_write_pass=pass_ctx
        )
        == 40
    )
    assert counts.get(engine.relationships_index, 0) == 1
    assert engine.fake.exists_calls == 0


# -- EML attachment knowledge extraction V1: server parity -------------------


def _email_with_attachments(
    file_id: str, document_id: str, names: list[str], *, generation: int
) -> PreparedDocument:
    """A parent email payload plus one child payload per ``names`` entry,
    all sharing ``file_id``/``generation`` (as ``documents.pipeline`` builds
    them).
    """
    parent = _document(file_id, document_id, [f"{document_id}-c"], generation=generation)
    assert parent.document is not None
    parent.document = parent.document.model_copy(update={"format": DocumentFormat.EML})
    for index, name in enumerate(names):
        child = _document(
            file_id, f"{document_id}-att{index}", [f"{document_id}-att{index}-c"],
            generation=generation,
        )
        assert child.document is not None
        child.document = child.document.model_copy(
            update={
                "format": DocumentFormat.TXT,
                "parent_document_id": document_id,
                "attachment_name": name,
                "attachment_content_type": "text/plain",
                "attachment_index": index,
            }
        )
        child.parent_title = f"Title {document_id}"
        parent.attachments.append(child)
    return parent


@pytest.mark.parametrize("engine_name", ENGINES)
def test_email_attachments_publish_with_distinct_ids_and_provenance(engine_name: str) -> None:
    # Generation "0" is the default published generation, so the read
    # contract (get_documents/list_documents) sees these rows.
    engine = _Engine(engine_name)
    backend = engine.backend
    backend.publish_document(
        _email_with_attachments("f1", "mail", ["a.txt", "a.txt"], generation=0)
    )

    documents = _content_by(engine, "document", "0")
    assert sorted(d["document_id"] for d in documents) == ["mail", "mail-att0", "mail-att1"]
    content_ids = set(engine.docs(engine.content_index))
    assert engine.ids.document_doc_id("s1", "f1", "0") in content_ids
    assert engine.ids.document_doc_id("s1", "f1", "0", attachment_index=0) in content_ids
    assert engine.ids.document_doc_id("s1", "f1", "0", attachment_index=1) in content_ids
    # Parent payload unchanged (no provenance keys at all).
    parent = next(d for d in documents if d["document_id"] == "mail")
    assert "parent_document_id" not in parent
    # Attachment chunks carry provenance for search results.
    att_chunk = next(
        c for c in _content_by(engine, "chunk", "0") if c["document_id"] == "mail-att1"
    )
    assert att_chunk["attachment_name"] == "a.txt"
    assert att_chunk["attachment_index"] == 1
    assert att_chunk["parent_title"] == "Title mail"
    assert att_chunk["attachment_format"] == "txt"

    records = {r.document_id: r for r in backend.get_documents(["mail", "mail-att0"])}
    assert records["mail"].parent_document_id is None
    assert records["mail-att0"].parent_document_id == "mail"
    assert records["mail-att0"].attachment_name == "a.txt"
    assert records["mail-att0"].attachment_index == 0
    listed = {r.document_id for r in backend.list_documents(source_id="s1")}
    assert listed == {"mail", "mail-att0", "mail-att1"}


@pytest.mark.parametrize("engine_name", ENGINES)
def test_email_reindex_with_removed_attachment_leaves_no_stale_child(engine_name: str) -> None:
    engine = _Engine(engine_name)
    backend = engine.backend
    backend.publish_document(
        _email_with_attachments("f1", "mail", ["keep.txt", "drop.txt"], generation=1)
    )
    # Incremental republish into the same generation, batch path.
    pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=False)
    backend.publish_document_batch(
        [_email_with_attachments("f1", "mail2", ["keep.txt"], generation=1)],
        server_write_pass=pass_ctx,
    )
    engine.fake.indices.refresh(index=engine.content_index)
    documents = _content_by(engine, "document", "1")
    assert sorted(d["document_id"] for d in documents) == ["mail2", "mail2-att0"]
    assert all(
        c["document_id"] in {"mail2", "mail2-att0"} for c in _content_by(engine, "chunk", "1")
    )


@pytest.mark.parametrize("engine_name", ENGINES)
def test_email_file_deletion_removes_attachment_children(engine_name: str) -> None:
    engine = _Engine(engine_name)
    backend = engine.backend
    _seed_source(engine, "s1", ["f1"])
    backend.publish_document(_email_with_attachments("f1", "mail", ["a.txt"], generation=1))
    backend.delete_files_batch("s1", ["f1"])
    assert [
        src for src in _artifacts_for(engine, "s1", "f1") if src.get("doc_kind") != "file"
    ] == []
