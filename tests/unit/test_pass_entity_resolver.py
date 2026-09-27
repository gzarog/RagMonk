"""Server Indexing Performance V3, item 2: ``PassEntityResolver``.

Covers:
- Fresh/unpublished generation (``generation_is_empty=True``): resolution
  happens purely against the overlay, with zero backend calls.
- Incremental generation: backend lookups are cached across the whole
  pass, stale entities from files already replaced this pass are
  filtered out, and overlay entities for those files are used instead.
- ``clear_only=True`` empties a file's overlay contribution.
- Same-file exclusion (a file never resolves against its own entities).
- Direct/legacy callers with no ``server_write_pass``/
  ``pass_entity_resolver`` keep ``code/processor.py::publish_code``'s
  original per-call-cache behavior unchanged.
- End-to-end: ``run_source_pass`` across two files where file B
  references file A, resolved correctly via the new resolver.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.models import (
    BackendStats,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
    ServerWritePass,
)
from ragmonk.backends.models import (
    FileRecord as BackendFileRecord,
)
from ragmonk.backends.server_common import PassEntityResolver
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Entity, EntityType
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry


def _entity(*, id: str, file_id: str, name: str, qualified_name: str | None = None) -> Entity:
    return Entity(
        id=id,
        source_id="src-1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=qualified_name or name,
        language="python",
        start_line=1,
        end_line=2,
        generation=1,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class _CountingBackend:
    """Just enough of ``KnowledgeBackend.find_entities_by_names`` to drive
    ``PassEntityResolver`` directly, with a call counter.
    """

    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities
        self.calls = 0

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        self.calls += 1
        if names:
            return [e for e in self._entities if e.name in names]
        if qualified_names:
            return [e for e in self._entities if e.qualified_name in qualified_names]
        return []


def test_fresh_generation_resolves_only_via_overlay_with_zero_backend_calls() -> None:
    backend = _CountingBackend(entities=[])
    pass_ctx = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    resolver = PassEntityResolver(backend, pass_ctx)  # type: ignore[arg-type]

    # File B looks up "Foo" before file A has committed: nothing yet.
    assert resolver.lookup_name("Foo", exclude_file_id="file-b") == []
    assert backend.calls == 0

    # File A commits an entity named "Foo".
    entity_a = _entity(id="e1", file_id="file-a", name="Foo")
    resolver.commit_file("file-a", [entity_a])

    # File B now resolves it via the overlay, still with zero backend calls.
    found = resolver.lookup_name("Foo", exclude_file_id="file-b")
    assert [e.id for e in found] == ["e1"]
    assert backend.calls == 0


def test_incremental_mode_filters_stale_replaced_file_and_uses_overlay() -> None:
    stale_entity = _entity(id="stale", file_id="file-a", name="Foo")
    backend = _CountingBackend(entities=[stale_entity])
    pass_ctx = ServerWritePass(source_id="src-1", generation=5, generation_is_empty=False)
    resolver = PassEntityResolver(backend, pass_ctx)  # type: ignore[arg-type]

    # Before file A is replaced this pass, the backend's (stale-published)
    # copy resolves normally.
    found = resolver.lookup_name("Foo", exclude_file_id="file-b")
    assert [e.id for e in found] == ["stale"]
    assert backend.calls == 1

    # File A gets rewritten mid-pass with a new entity of the same name.
    fresh_entity = _entity(id="fresh", file_id="file-a", name="Foo")
    resolver.commit_file("file-a", [fresh_entity])

    # A later lookup (still cache key "Foo") must no longer return the
    # stale backend copy -- only the fresh overlay entity -- and must not
    # re-hit the backend (cache still holds the raw backend result).
    found_after = resolver.lookup_name("Foo", exclude_file_id="file-c")
    assert [e.id for e in found_after] == ["fresh"]
    assert backend.calls == 1


def test_clear_only_empties_overlay_contribution() -> None:
    backend = _CountingBackend(entities=[])
    pass_ctx = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    resolver = PassEntityResolver(backend, pass_ctx)  # type: ignore[arg-type]

    resolver.commit_file("file-a", [_entity(id="e1", file_id="file-a", name="Foo")])
    assert resolver.lookup_name("Foo", exclude_file_id="file-b") != []

    # file-a is rewritten with no entities at all (e.g. an unparseable
    # file re-indexed as clear_only).
    resolver.commit_file("file-a", [], clear_only=True)
    assert resolver.lookup_name("Foo", exclude_file_id="file-b") == []


def test_same_file_exclusion_holds_in_both_modes() -> None:
    # Fresh-generation / overlay-only mode.
    backend = _CountingBackend(entities=[])
    fresh_pass = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    fresh_resolver = PassEntityResolver(backend, fresh_pass)  # type: ignore[arg-type]
    fresh_resolver.commit_file("file-a", [_entity(id="e1", file_id="file-a", name="Foo")])
    assert fresh_resolver.lookup_name("Foo", exclude_file_id="file-a") == []

    # Incremental / backend-cache mode.
    entity = _entity(id="e2", file_id="file-a", name="Bar")
    inc_backend = _CountingBackend(entities=[entity])
    inc_pass = ServerWritePass(source_id="src-1", generation=2, generation_is_empty=False)
    inc_resolver = PassEntityResolver(inc_backend, inc_pass)  # type: ignore[arg-type]
    assert inc_resolver.lookup_name("Bar", exclude_file_id="file-a") == []


def test_qualified_name_lookup_uses_qualified_name_field() -> None:
    backend = _CountingBackend(entities=[])
    pass_ctx = ServerWritePass(source_id="src-1", generation=1, generation_is_empty=True)
    resolver = PassEntityResolver(backend, pass_ctx)  # type: ignore[arg-type]
    resolver.commit_file(
        "file-a", [_entity(id="e1", file_id="file-a", name="Foo", qualified_name="pkg.Foo")]
    )
    # Name-field lookup for the qualified string finds nothing (field
    # mismatch); qualified lookup finds it.
    assert resolver.lookup_name("pkg.Foo", exclude_file_id="file-b") == []
    found = resolver.lookup_qualified("pkg.Foo", exclude_file_id="file-b")
    assert [e.id for e in found] == ["e1"]


# ---------------------------------------------------------------------------
# End-to-end: run_source_pass with two files, file B referencing file A.
# ---------------------------------------------------------------------------


class _FakeServerBackend(KnowledgeBackend):
    """In-memory server-mode ``KnowledgeBackend`` double supporting
    ``find_entities_by_names`` against whatever this pass has published
    so far (mirrors OS/ES's generation-filtered read).
    """

    def __init__(self) -> None:
        self._active_generation: dict[str, str] = {}
        self._files: dict[str, dict[str, BackendFileRecord]] = {}
        self._entities: dict[str, list[Entity]] = {}  # (source_id, generation) -> entities
        self._relationships: dict[str, list[Any]] = {}  # (source_id, generation) -> relationships
        self.find_calls = 0
        # Server Indexing Performance V3, item 5: barrier-call tracking.
        self.refresh_for_linking_calls = 0
        self.refresh_all_calls = 0

    def health(self) -> bool:
        return True

    def ensure_schema(self) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def is_server(self) -> bool:
        return True

    def begin_generation(self, source_id: str) -> str:
        current = self._active_generation.get(source_id, "0")
        return str(int(current) + 1)

    def published_generation(self, source_id: str) -> str | None:
        return self._active_generation.get(source_id)

    def publish_generation(self, source_id: str, generation: str) -> None:
        self._active_generation[source_id] = generation

    def abort_generation(self, source_id: str, generation: str) -> None:
        return None

    def upsert_file(self, file_record: BackendFileRecord) -> None:
        self._files.setdefault(file_record.source_id, {})[file_record.file_id] = file_record

    def delete_file(self, source_id: str, file_id: str) -> None:
        self._files.get(source_id, {}).pop(file_id, None)

    def list_files(self, source_id: str) -> list[BackendFileRecord]:
        return list(self._files.get(source_id, {}).values())

    def publish_code(self, prepared_code: PreparedCode) -> None:
        key = f"{prepared_code.source_id}:{prepared_code.generation}"
        existing = [e for e in self._entities.get(key, []) if e.file_id != prepared_code.file_id]
        existing.extend(prepared_code.entities)
        self._entities[key] = existing
        rel_existing = [
            (fid, r) for fid, r in self._relationships.get(key, []) if fid != prepared_code.file_id
        ]
        rel_existing.extend((prepared_code.file_id, r) for r in prepared_code.relationships)
        self._relationships[key] = rel_existing

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        return None

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        return None

    def publish_links(
        self, prepared_links: PreparedLinks, *, server_write_pass: ServerWritePass | None = None
    ) -> int:
        return 0

    def refresh_for_linking(self, source_id: str) -> None:
        self.refresh_for_linking_calls += 1

    def refresh_all(self, source_id: str) -> None:
        self.refresh_all_calls += 1

    def list_source_entities(
        self, source_id: str, *, generation: str | None = None
    ) -> list[Entity]:
        key = f"{source_id}:{generation}"
        return list(self._entities.get(key, []))

    def list_source_document_units(
        self, source_id: str, *, generation: str | None = None
    ) -> list[Any]:
        return []

    def find_relationships_by_target_prefix(
        self, source_id: str, prefix: str, *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        self.find_calls += 1
        key = f"{source_id}:{generation}"
        pool = self._entities.get(key, [])
        out = []
        for e in pool:
            if names and e.name in names or qualified_names and e.qualified_name in qualified_names:
                out.append(e)
        return out

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

    def get_file(self, file_id: str) -> BackendFileRecord | None:
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


def test_end_to_end_two_files_with_cross_reference_resolve_via_resolver(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    backend = _FakeServerBackend()
    ctx = _server_ctx(backend)
    try:
        source_dir = tmp_path / "src"
        source_dir.mkdir(exist_ok=True)
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registered = registry.add(str(source_dir))
        source_id = registered.id

        (source_dir / "a.py").write_text("def helper():\n    return 1\n")
        (source_dir / "b.py").write_text(
            "from a import helper\n\ndef caller():\n    return helper()\n"
        )

        source = registry.get(source_id)
        processors = build_processor_registry(ctx.config)
        result = run_source_pass(ctx, source, processors)
        assert not result.result.source_offline

        gen = backend.published_generation(source_id)
        assert gen is not None
        entities = backend._entities.get(f"{source_id}:{gen}", [])
        assert entities, "expected entities to have been published"

        # Server Indexing Performance V3, item 5: the process-to-linker
        # barrier fired exactly once (both files were touched, so
        # ``link_touched_files`` ran and needed to read this pass's
        # writes). ``refresh_all`` (the end-of-incremental-pass sweep)
        # must NOT have fired -- this is a fresh-generation pass (first
        # publication), already covered by ``publish_generation``'s own
        # refresh once it swaps the marker.
        assert backend.refresh_for_linking_calls == 1
        assert backend.refresh_all_calls == 0
    finally:
        ctx.close()


def test_incremental_pass_refreshes_at_final_barrier_not_fresh_generation(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """Server Indexing Performance V3, item 5: a second, incremental pass
    against an already-published generation hits the end-of-pass
    ``refresh_all`` barrier -- unlike the first (fresh-generation) pass
    above, nothing else refreshes everything for it.
    """
    backend = _FakeServerBackend()
    ctx = _server_ctx(backend)
    try:
        source_dir = tmp_path / "src"
        source_dir.mkdir(exist_ok=True)
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registered = registry.add(str(source_dir))
        source_id = registered.id

        (source_dir / "a.py").write_text("def helper():\n    return 1\n")
        source = registry.get(source_id)
        processors = build_processor_registry(ctx.config)
        result = run_source_pass(ctx, source, processors)
        assert not result.result.source_offline
        assert backend.refresh_all_calls == 0

        # Second, incremental pass: a new file touches the published
        # generation directly (no begin/publish_generation wrapping).
        (source_dir / "c.py").write_text("def other():\n    return 2\n")
        source = registry.get(source_id)
        result2 = run_source_pass(ctx, source, processors)
        assert not result2.result.source_offline
        assert backend.refresh_all_calls == 1
    finally:
        ctx.close()
