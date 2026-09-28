"""Server Indexing Performance V3 completion: coordinator and resolver
regression coverage.

Covers:
- P1: server CODE and DOCUMENT batching in both the default serial
  (workers=1) path and the optional parallel path; bounded document
  batches; a failed document batch never produces INDEXED bookkeeping;
  local mode keeps its per-file publication.
- P2: ``PassEntityResolver``'s reversible staged overlay -- same-batch
  visibility, stale-backend filtering, ``clear_only`` as a zero-entity
  replacement, discard on failure and promotion on commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from tests.unit._fake_opensearch import FakeOpenSearch
from tests.unit.test_index_coordinator_code_batching import (
    _build_coordinator,
    _code_only_registry,
    _FakeServerBackend,
    _write_project,
)
from tests.unit.test_index_coordinator_parallel_documents import (
    _config as _docs_config,
)
from tests.unit.test_index_coordinator_parallel_documents import (
    _documents_only_registry,
)
from tests.unit.test_index_coordinator_parallel_documents import (
    _write_project as _write_doc_project,
)

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.backends.models import ServerWritePass
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.backends.server_common import PassEntityResolver
from ragmonk.code.processor import code_processor, prepare_code, publish_code
from ragmonk.core.config import IndexingConfig, RagMonkConfig, ServerStorageConfig
from ragmonk.core.models import Entity, EntityType, FileKind, FileStatus
from ragmonk.indexing.coordinator import IndexCoordinator, ProcessorRegistry, raw_processor
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect

# -- P1.1: code batching -----------------------------------------------------


def test_default_workers_1_server_code_batching_uses_one_batch_call(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 6)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        result = _build_coordinator(conn, root, backend, workers=1).run()

        assert result.indexed == 6
        assert result.failed == 0
        assert backend.single_calls == []
        assert len(backend.batch_calls) == 1
        assert len(backend.batch_calls[0]) == 6
    finally:
        conn.close()


def test_parallel_server_code_batching_still_combines_files(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 6)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        result = _build_coordinator(conn, root, backend, workers=3).run()

        assert result.indexed == 6
        assert backend.single_calls == []
        assert sum(len(call) for call in backend.batch_calls) == 6
        assert any(len(call) > 1 for call in backend.batch_calls)
    finally:
        conn.close()


def test_serial_server_code_oversized_file_is_skipped_not_batched(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 2)
    (root / "pkg" / "big.py").write_text("x = 1\n" * 400_000)  # ~2.4 MB
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        coord = _build_coordinator(conn, root, backend, workers=1)
        coord._config = coord._config.model_copy(
            update={"indexing": coord._config.indexing.model_copy(update={"max_file_size_mb": 1})}
        )
        result = coord.run()

        assert result.indexed == 2
        assert result.skipped_limit == 1
        batched = {fid for call in backend.batch_calls for fid in call}
        assert len(batched) == 2
    finally:
        conn.close()


def test_failed_code_batch_discards_staged_resolver_state(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 3)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend(fail=True)
        coord = _build_coordinator(conn, root, backend, workers=1)
        resolver = coord._pass_entity_resolver
        assert resolver is not None
        result = coord.run()

        assert result.indexed == 0
        assert all(
            f.status is not FileStatus.INDEXED for f in files_repo.list_by_source(conn, "s1")
        )
        assert resolver._staged_overlay == {}
        assert resolver._staged_replaced_file_ids == set()
        assert resolver._overlay == {}
        assert resolver._replaced_file_ids == set()
    finally:
        conn.close()


def test_successful_code_batch_promotes_staged_state_to_committed(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 3)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        coord = _build_coordinator(conn, root, backend, workers=1)
        resolver = coord._pass_entity_resolver
        assert resolver is not None
        coord.run()

        assert resolver._staged_overlay == {}
        assert len(resolver._overlay) == 3
    finally:
        conn.close()


# -- local mode ---------------------------------------------------------------


def test_local_mode_serial_path_publishes_per_file_without_batching(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 4)
    calls: list[str] = []

    def counting_code_processor(ctx: Any) -> Any:
        calls.append(str(ctx.path))
        return code_processor(ctx)

    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    registry.register(
        FileKind.CODE, counting_code_processor, prepare=prepare_code, publish=publish_code
    )
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        config = RagMonkConfig(indexing=IndexingConfig(code_extraction_workers=1))
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        assert result.indexed == 4
        # Every file went through the registered per-file processor.
        assert len(calls) == 4
        assert coord._code_batch == []
        assert coord._document_batch == []
        assert coord._server_batching_enabled(FileKind.CODE) is False
        assert coord._server_batching_enabled(FileKind.DOCUMENT) is False
    finally:
        conn.close()


# -- P1.2/P1.3: document batching ---------------------------------------------


def _server_doc_coordinator(
    conn: sqlite3.Connection, root: Path, *, workers: int, fake: FakeOpenSearch
) -> tuple[IndexCoordinator, OpenSearchKnowledgeBackend]:
    backend = OpenSearchKnowledgeBackend(
        ServerStorageConfig(engine="opensearch", url="http://fake:9200"), client=fake
    )
    pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=True)
    coord = IndexCoordinator(
        conn,
        "s1",
        str(root),
        [],
        [],
        _docs_config(document_extraction_workers=workers),
        processors=_documents_only_registry(),
        backend=backend,
        force_generation=1,
        server_write_pass=pass_ctx,
    )
    return coord, backend


def _document_ids(fake: FakeOpenSearch) -> set[str]:
    return {
        src["file_id"]
        for store in fake.store.values()
        for src in store.values()
        if src.get("doc_kind") == "document"
    }


def test_default_workers_1_server_document_batching(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _write_doc_project(root, 4)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        fake = FakeOpenSearch()
        coord, backend = _server_doc_coordinator(conn, root, workers=1, fake=fake)
        calls: list[int] = []
        original = backend.publish_document_batch

        def spy(items: list[Any], **kwargs: Any) -> None:
            calls.append(len(items))
            original(items, **kwargs)

        backend.publish_document_batch = spy  # type: ignore[method-assign]
        result = coord.run()

        assert result.indexed == 4
        assert calls == [4]
        assert len(_document_ids(fake)) == 4
    finally:
        conn.close()


def test_parallel_server_document_batching_still_works(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _write_doc_project(root, 5)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        fake = FakeOpenSearch()
        coord, backend = _server_doc_coordinator(conn, root, workers=2, fake=fake)
        calls: list[int] = []
        original = backend.publish_document_batch

        def spy(items: list[Any], **kwargs: Any) -> None:
            calls.append(len(items))
            original(items, **kwargs)

        backend.publish_document_batch = spy  # type: ignore[method-assign]
        result = coord.run()

        assert result.indexed == 5
        assert result.failed == 0
        assert calls == [5]
        assert len(_document_ids(fake)) == 5
        assert len(result.touched_document_file_ids) == 5
    finally:
        conn.close()


def test_document_batch_respects_file_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "_DOCUMENT_BATCH_MAX_FILES", 2)
    root = tmp_path / "docs"
    _write_doc_project(root, 5)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        fake = FakeOpenSearch()
        coord, backend = _server_doc_coordinator(conn, root, workers=1, fake=fake)
        calls: list[int] = []
        original = backend.publish_document_batch

        def spy(items: list[Any], **kwargs: Any) -> None:
            calls.append(len(items))
            original(items, **kwargs)

        backend.publish_document_batch = spy  # type: ignore[method-assign]
        result = coord.run()

        assert result.indexed == 5
        assert calls == [2, 2, 1]
    finally:
        conn.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_document_batch_failure_marks_no_file_indexed(tmp_path: Path, workers: int) -> None:
    root = tmp_path / "docs"
    _write_doc_project(root, 3)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        fake = FakeOpenSearch()
        coord, backend = _server_doc_coordinator(conn, root, workers=workers, fake=fake)

        def boom(items: list[Any], **kwargs: Any) -> None:
            raise RuntimeError("simulated document batch failure")

        backend.publish_document_batch = boom  # type: ignore[method-assign]
        result = coord.run()

        assert result.indexed == 0
        assert result.touched_document_file_ids == []
        statuses = {f.status for f in files_repo.list_by_source(conn, "s1")}
        assert FileStatus.INDEXED not in statuses
        assert statuses == {FileStatus.RETRY}
    finally:
        conn.close()


# -- P2: PassEntityResolver staged overlay ------------------------------------


def _resolver_entity(entity_id: str, file_id: str, name: str = "helper") -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=f"mod.{name}",
        language="python",
        start_line=1,
        end_line=2,
        generation=0,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


class _BackendWithPublished(_FakeServerBackend):
    """Returns a fixed set of already-published entities for every lookup."""

    def __init__(self, published: list[Entity]) -> None:
        super().__init__()
        self._published = published

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        self.find_calls += 1
        wanted = set(names or []) | set(qualified_names or [])
        return [e for e in self._published if e.name in wanted or e.qualified_name in wanted]


def _incremental_resolver(published: list[Entity]) -> PassEntityResolver:
    return PassEntityResolver(
        _BackendWithPublished(published),
        ServerWritePass(source_id="s1", generation=0, generation_is_empty=False),
    )


def test_staged_file_is_visible_to_later_files_before_flush() -> None:
    resolver = PassEntityResolver(
        _FakeServerBackend(),
        ServerWritePass(source_id="s1", generation=1, generation_is_empty=True),
    )
    resolver.stage_file("fa", [_resolver_entity("new-a", "fa")])

    assert [e.id for e in resolver.lookup_name("helper", exclude_file_id="fb")] == ["new-a"]
    # A file never resolves against itself.
    assert resolver.lookup_name("helper", exclude_file_id="fa") == []


def test_staged_replacement_filters_stale_backend_entities() -> None:
    resolver = _incremental_resolver([_resolver_entity("old-a", "fa")])
    assert [e.id for e in resolver.lookup_name("helper", exclude_file_id="fb")] == ["old-a"]

    resolver.stage_file("fa", [_resolver_entity("new-a", "fa")])
    assert [e.id for e in resolver.lookup_name("helper", exclude_file_id="fb")] == ["new-a"]


def test_clear_only_staged_replacement_hides_backend_entities() -> None:
    resolver = _incremental_resolver([_resolver_entity("old-a", "fa")])
    resolver.stage_file("fa", [_resolver_entity("ignored", "fa")], clear_only=True)

    # clear_only is a logical replacement with zero entities.
    assert resolver.lookup_name("helper", exclude_file_id="fb") == []
    assert resolver.lookup_qualified("mod.helper", exclude_file_id="fb") == []


def test_discard_restores_pre_stage_view() -> None:
    resolver = _incremental_resolver([_resolver_entity("old-a", "fa")])
    resolver.stage_file("fa", [_resolver_entity("new-a", "fa")])
    resolver.discard_file("fa")

    # The failed batch leaves no staged state behind; the durable
    # published copy is what resolution sees again.
    assert resolver._staged_overlay == {}
    assert resolver._staged_replaced_file_ids == set()
    assert [e.id for e in resolver.lookup_name("helper", exclude_file_id="fb")] == ["old-a"]


def test_commit_promotes_staged_state() -> None:
    resolver = _incremental_resolver([_resolver_entity("old-a", "fa")])
    resolver.stage_file("fa", [_resolver_entity("new-a", "fa")])
    resolver.commit_file("fa", [_resolver_entity("new-a", "fa")])

    assert resolver._staged_overlay == {}
    assert resolver._replaced_file_ids == {"fa"}
    assert [e.id for e in resolver.lookup_name("helper", exclude_file_id="fb")] == ["new-a"]


def test_same_unflushed_batch_resolution_incremental_without_refresh(tmp_path: Path) -> None:
    """Incremental pass: b.py resolves a.py's *new* entity from the same
    unflushed batch, never the stale published copy, with no refresh or
    flush between the two files.
    """
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "a.py").write_text("def helper():\n    return 1\n")
    (root / "b.py").write_text("from a import helper\n\ndef caller():\n    return helper()\n")
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _BackendWithPublished([])
        pass_ctx = ServerWritePass(source_id="s1", generation=1, generation_is_empty=False)
        resolver = PassEntityResolver(backend, pass_ctx)
        coord = IndexCoordinator(
            conn,
            "s1",
            str(root),
            [],
            [],
            RagMonkConfig(indexing=IndexingConfig(code_extraction_workers=1)),
            processors=_code_only_registry(),
            backend=backend,
            server_write_pass=pass_ctx,
            pass_entity_resolver=resolver,
        )
        # Stand-in for a stale published copy of a.py's helper, keyed by
        # the file id a.py is about to get.
        original_stage = resolver.stage_file

        def stage_and_publish_stale(file_id: str, entities: list[Entity], **kw: Any) -> None:
            if any(e.name == "helper" for e in entities):
                backend._published.append(_resolver_entity("stale-helper", file_id))
            original_stage(file_id, entities, **kw)

        resolver.stage_file = stage_and_publish_stale  # type: ignore[method-assign]
        result = coord.run()

        assert result.indexed == 2
        assert len(backend.batch_calls) == 1
        files = {Path(f.path).name: f.id for f in files_repo.list_by_source(conn, "s1")}
        helper = next(e for e in backend._entities[files["a.py"]] if e.name == "helper")
        targets = {r.target_entity_id for r in backend._relationships[files["b.py"]]}
        assert helper.id in targets
        assert "stale-helper" not in targets
    finally:
        conn.close()
