"""Server Indexing Performance V3, item 3: ``IndexCoordinator``'s
batched CODE publish in server mode.

Covers:
- A batch flush combines actions from multiple distinct file_ids into
  ONE ``publish_code_batch`` call (not one per file).
- The batch bound (hard file-count cap) is respected: exceeding it
  triggers a flush at the right point, not sooner and not letting the
  buffer grow unbounded.
- A batch failure marks zero files from that batch as indexed, and the
  resolver overlay never sees ``commit_file`` called for any of them.
- A file modified during the prepare-to-publish window is still safely
  retried, never silently included in a stale batch.
- Local mode (``backend=None``, no ``server_write_pass``) is completely
  unaffected -- CODE files still publish immediately, one at a time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
from ragmonk.code.processor import code_processor, prepare_code, publish_code
from ragmonk.core.config import IndexingConfig, RagMonkConfig
from ragmonk.core.models import Entity, FileKind, FileStatus
from ragmonk.indexing.coordinator import IndexCoordinator, ProcessorRegistry, raw_processor
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect


def _config(*, code_extraction_workers: int = 4) -> RagMonkConfig:
    config = RagMonkConfig(
        indexing=IndexingConfig(code_extraction_workers=code_extraction_workers)
    )
    disabled_documents = config.documents.model_copy(update={"enabled": False})
    return config.model_copy(update={"documents": disabled_documents})


def _code_only_registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    registry.register(
        FileKind.CODE, code_processor, prepare=prepare_code, publish=publish_code
    )
    return registry


def _write_project(root: Path, n_files: int) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (pkg / f"mod_{i}.py").write_text(f"def helper_{i}():\n    return {i}\n")


class _FakeServerBackend(KnowledgeBackend):
    """Minimal in-memory server-mode ``KnowledgeBackend`` double whose
    ``publish_code_batch`` records exactly which file_ids were combined
    into each call, and can be made to fail on demand.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []
        self._entities: dict[str, list[Entity]] = {}
        self._fail = fail

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
        return "1"

    def published_generation(self, source_id: str) -> str | None:
        return "0"

    def publish_generation(self, source_id: str, generation: str) -> None:
        return None

    def abort_generation(self, source_id: str, generation: str) -> None:
        return None

    def upsert_file(self, file_record: BackendFileRecord) -> None:
        return None

    def delete_file(self, source_id: str, file_id: str) -> None:
        return None

    def publish_code(self, prepared_code: PreparedCode) -> None:
        self.single_calls.append(prepared_code.file_id)
        self._entities[prepared_code.file_id] = list(prepared_code.entities)

    def publish_code_batch(
        self,
        items: list[PreparedCode],
        *,
        server_write_pass: ServerWritePass | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("simulated batch write failure")
        self.batch_calls.append([item.file_id for item in items])
        for item in items:
            self._entities[item.file_id] = list(item.entities)

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        return None

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        return None

    def publish_links(self, prepared_links: PreparedLinks) -> int:
        return 0

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        return []

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


def _build_coordinator(
    conn, root: Path, backend: _FakeServerBackend | None, *, workers: int = 4
) -> IndexCoordinator:
    config = _config(code_extraction_workers=workers)
    registry = _code_only_registry()
    server_write_pass = None
    pass_entity_resolver = None
    if backend is not None:
        server_write_pass = ServerWritePass(
            source_id="s1", generation=1, generation_is_empty=True
        )
        pass_entity_resolver = PassEntityResolver(backend, server_write_pass)
    return IndexCoordinator(
        conn,
        "s1",
        str(root),
        [],
        [],
        config,
        processors=registry,
        backend=backend,
        server_write_pass=server_write_pass,
        pass_entity_resolver=pass_entity_resolver,
    )


def test_batch_flush_combines_multiple_files_into_one_backend_call(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 5)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        coord = _build_coordinator(conn, root, backend, workers=4)
        result = coord.run()

        assert result.indexed == 5
        assert result.failed == 0
        # Every file went through the batch path, never the one-item path.
        assert backend.single_calls == []
        # At least one batch combined more than one file.
        assert any(len(call) > 1 for call in backend.batch_calls)
        all_batched_ids = {fid for call in backend.batch_calls for fid in call}
        assert len(all_batched_ids) == 5
    finally:
        conn.close()


def test_local_mode_is_unaffected_by_batching(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 5)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        coord = _build_coordinator(conn, root, backend=None, workers=4)
        result = coord.run()
        assert result.indexed == 5
        assert result.failed == 0
        files = files_repo.list_by_source(conn, "s1")
        assert all(f.status is FileStatus.INDEXED for f in files)
    finally:
        conn.close()


def test_batch_bound_hard_file_cap_forces_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ragmonk.indexing.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "_CODE_BATCH_MAX_FILES", 2)

    root = tmp_path / "proj"
    _write_project(root, 6)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        coord = _build_coordinator(conn, root, backend, workers=4)
        result = coord.run()

        assert result.indexed == 6
        # With the cap lowered to 2, no single batch call may exceed it.
        assert all(len(call) <= 2 for call in backend.batch_calls)
        assert len(backend.batch_calls) >= 3
    finally:
        conn.close()


def test_failed_batch_marks_no_file_indexed_and_no_resolver_commit(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write_project(root, 3)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend(fail=True)
        coord = _build_coordinator(conn, root, backend, workers=4)
        result = coord.run()

        assert result.indexed == 0
        # RETRY (not permanent yet on first attempt) -- none stayed
        # PROCESSING and none became INDEXED.
        files = files_repo.list_by_source(conn, "s1")
        assert all(f.status is not FileStatus.INDEXED for f in files)
        # The resolver never saw a committed file from the failed batch.
        assert backend._entities == {}
    finally:
        conn.close()


def test_file_modified_during_prepare_to_publish_window_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    _write_project(root, 3)
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn, "knowledge")
        backend = _FakeServerBackend()
        coord = _build_coordinator(conn, root, backend, workers=1)

        # Force the first prepared file's on-disk content to change right
        # before finalize (phase a) runs, by mutating mtime/size after
        # scan/claim but simulated here via monkeypatching stat_unchanged
        # to report a mismatch for exactly one call.
        import ragmonk.code.processor as processor_module

        calls = {"n": 0}
        real_stat_unchanged = processor_module.stat_unchanged

        def flaky_stat_unchanged(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            return real_stat_unchanged(*args, **kwargs)

        monkeypatch.setattr(processor_module, "stat_unchanged", flaky_stat_unchanged)

        result = coord.run()
        # The flaky file was retried (RETRY), the other files succeeded.
        assert result.indexed == 2
        files = files_repo.list_by_source(conn, "s1")
        retried = [f for f in files if f.status is FileStatus.RETRY]
        assert len(retried) == 1
    finally:
        conn.close()
