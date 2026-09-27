"""Server Indexing Performance V3, item 1: ``ServerWritePass`` construction
and threading.

Covers:
- ``ServerWritePass`` constructs with the documented fields/defaults.
- ``indexing/runner.py`` builds one per server-mode pass with
  ``generation_is_empty`` set correctly: ``True`` for a source's first
  publication (a fresh, unpublished generation), ``False`` for a later
  incremental pass against the already-published generation.
- Existing direct calls with no pass context (``backend.publish_code(...)``
  with no ``ServerWritePass``, ``link_touched_files(...)`` with no
  ``server_write_pass``) are unaffected -- the parameter is optional and
  unused by any real logic yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
    ServerWritePass,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing import coordinator as coordinator_module
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry


def test_server_write_pass_constructs_with_expected_fields_and_defaults() -> None:
    pass_ctx = ServerWritePass(source_id="src-1", generation=3, generation_is_empty=True)

    assert pass_ctx.source_id == "src-1"
    assert pass_ctx.generation == 3
    assert pass_ctx.generation_is_empty is True
    # Telemetry counters default to zero and are freely mutable -- not
    # wired to any real logic yet, but present for a future V3 item.
    assert pass_ctx.bulk_actions == 0
    assert pass_ctx.bulk_requests == 0
    assert pass_ctx.delete_by_query_count == 0
    assert pass_ctx.refresh_count == 0
    pass_ctx.bulk_actions += 5
    assert pass_ctx.bulk_actions == 5


def test_server_write_pass_is_freshly_constructed_per_pass_not_shared() -> None:
    a = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    b = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    assert a is not b
    a.bulk_actions = 42
    assert b.bulk_actions == 0


class _FakeServerBackend(KnowledgeBackend):
    """A minimal in-memory server-mode ``KnowledgeBackend`` double, just
    enough to drive ``run_source_pass`` through both the "first
    publication" and "incremental against the published generation"
    branches (see ``indexing/runner.py``).
    """

    def __init__(self) -> None:
        self._active_generation: dict[str, str] = {}
        self._files: dict[str, dict[str, FileRecord]] = {}
        self.begin_calls: list[str] = []

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        return True

    def ensure_schema(self) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def is_server(self) -> bool:
        return True

    # -- generation lifecycle -------------------------------------------
    def begin_generation(self, source_id: str) -> str:
        self.begin_calls.append(source_id)
        current = self._active_generation.get(source_id, "0")
        return str(int(current) + 1)

    def published_generation(self, source_id: str) -> str | None:
        return self._active_generation.get(source_id)

    def publish_generation(self, source_id: str, generation: str) -> None:
        self._active_generation[source_id] = generation

    def abort_generation(self, source_id: str, generation: str) -> None:
        return None

    # -- writes -----------------------------------------------------------
    def upsert_file(self, file_record: FileRecord) -> None:
        self._files.setdefault(file_record.source_id, {})[file_record.file_id] = file_record

    def delete_file(self, source_id: str, file_id: str) -> None:
        self._files.get(source_id, {}).pop(file_id, None)

    def list_files(self, source_id: str) -> list[FileRecord]:
        return list(self._files.get(source_id, {}).values())

    def publish_code(self, prepared_code: PreparedCode) -> None:
        return None

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        return None

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        return None

    def publish_links(self, prepared_links: PreparedLinks) -> int:
        return 0

    # -- reads / search ---------------------------------------------------
    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        return []

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        return []

    def symbol_search(self, name: str, filters: dict[str, Any] | None = None) -> list[SearchHit]:
        return []

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        return []

    def get_file(self, file_id: str) -> FileRecord | None:
        return None

    def get_entities_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_document_units_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def count_stats(self) -> BackendStats:
        return BackendStats()

    def clear_source(self, source_id: str) -> None:
        return None


def _server_ctx(backend: KnowledgeBackend) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    ctx._server_backend = backend
    return ctx


def _register_source(ctx: AppContext, tmp_path: Path) -> str:
    source_dir = tmp_path / "src"
    source_dir.mkdir(exist_ok=True)
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    source = registry.add(str(source_dir))
    return source.id


def test_first_publication_pass_has_generation_is_empty_true(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    captured: list[ServerWritePass | None] = []
    real_init = coordinator_module.IndexCoordinator.__init__

    def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.append(kwargs.get("server_write_pass"))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(coordinator_module.IndexCoordinator, "__init__", _capturing_init)

    backend = _FakeServerBackend()
    ctx = _server_ctx(backend)
    try:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source_id = _register_source(ctx, tmp_path)
        source = registry.get(source_id)
        processors = build_processor_registry(ctx.config)
        run_source_pass(ctx, source, processors)
    finally:
        ctx.close()

    assert len(captured) == 1
    pass_ctx = captured[0]
    assert pass_ctx is not None
    assert pass_ctx.source_id == source_id
    assert pass_ctx.generation_is_empty is True


def test_incremental_pass_against_published_generation_has_generation_is_empty_false(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    backend = _FakeServerBackend()
    ctx = _server_ctx(backend)
    try:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source_id = _register_source(ctx, tmp_path)
        source = registry.get(source_id)
        processors = build_processor_registry(ctx.config)
        # First pass publishes the source's first generation.
        run_source_pass(ctx, source, processors)
        assert backend.published_generation(source_id) is not None

        captured: list[ServerWritePass | None] = []
        real_init = coordinator_module.IndexCoordinator.__init__

        def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
            captured.append(kwargs.get("server_write_pass"))
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(coordinator_module.IndexCoordinator, "__init__", _capturing_init)

        # Second pass: nothing changed, but the source now has a
        # published generation, so this is the incremental branch.
        run_source_pass(ctx, source, processors)
    finally:
        ctx.close()

    assert len(captured) == 1
    pass_ctx = captured[0]
    assert pass_ctx is not None
    assert pass_ctx.source_id == source_id
    assert pass_ctx.generation_is_empty is False


def test_publish_code_and_link_touched_files_still_work_with_no_pass_context() -> None:
    """Every existing direct-call site keeps working unchanged: the new
    ``server_write_pass``/``ProcessorContext.server_write_pass`` parameters
    default to ``None`` and are not required anywhere.
    """
    from ragmonk.knowledge.linker import link_touched_files
    from ragmonk.storage.sqlite import connect

    backend = _FakeServerBackend()
    # publish_code with no pass context at all (plain positional call).
    backend.publish_code(PreparedCode(file_id="f1", source_id="src-1"))

    conn = connect(Path(":memory:"))
    try:
        # link_touched_files with no server_write_pass kwarg -- must not
        # raise, and must behave exactly as it did before this change
        # (no touched files means an immediate no-op return of 0).
        linked = link_touched_files(
            conn,
            backend,
            source_id="src-1",
            touched_code_file_ids=[],
            touched_document_file_ids=[],
        )
    finally:
        conn.close()

    assert linked == 0
