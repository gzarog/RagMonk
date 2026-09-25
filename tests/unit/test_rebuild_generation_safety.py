"""Storage backend abstraction plan, Phase 7: server-mode ``ragmonk
rebuild`` generation-lifecycle safety.

Covers:
- A full server-mode rebuild opens exactly one generation
  (``begin_generation``), publishes some content through it, then calls
  ``publish_generation`` exactly once and never ``abort_generation``.
- A rebuild that fails partway (a fake backend raises on its Nth
  ``publish_code`` call) calls ``abort_generation`` with the exact
  ``(source_id, generation)`` it began, never calls
  ``publish_generation``, and the previously active generation's data
  (what a reader would see) is left exactly as it was.
- Retrying a failed rebuild (calling ``rebuild`` again for the same
  source) begins a fresh generation and succeeds without leaving
  duplicate or orphaned data -- a brief note, not a full test, on why
  this is safe by construction (see the module docstring below the
  tests).

The fake backend mirrors the documented OpenSearch/Elasticsearch
contract closely enough to make these assertions meaningful: every
content write is tagged with its generation, and "current state" reads
only ever see the marked *active* generation's documents, exactly like
``OpenSearchKnowledgeBackend``'s generation marker + filtered reads
(see that module's docstring) -- so "abort preserves the previous
generation" is asserted against actual read behavior, not just a call
count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.ops.rebuild import rebuild as run_rebuild
from ragmonk.sources.registry import SourceRegistry


class _FakeGenerationBackend(KnowledgeBackend):
    """An in-memory ``KnowledgeBackend`` double that actually implements
    the generation marker + tagged-document contract the real
    OpenSearch/Elasticsearch adapters document (P4/P5), so tests can
    assert on genuine read-visibility, not just call counts.
    """

    def __init__(self, *, fail_on_nth_publish_code: int | None = None) -> None:
        self._active_generation: dict[str, str] = {}
        # source_id -> generation -> {file_id: PreparedCode-ish payload}
        self._docs: dict[str, dict[str, dict[str, Any]]] = {}
        self._fail_on_nth_publish_code = fail_on_nth_publish_code
        self.publish_code_calls = 0
        self.begin_calls: list[str] = []
        self.publish_generation_calls: list[tuple[str, str]] = []
        self.abort_generation_calls: list[tuple[str, str]] = []

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        return True

    def ensure_schema(self) -> None:
        return None

    def close(self) -> None:
        return None

    # -- generation lifecycle -------------------------------------------
    def begin_generation(self, source_id: str) -> str:
        self.begin_calls.append(source_id)
        current = self._active_generation.get(source_id, "0")
        return str(int(current) + 1)

    def publish_generation(self, source_id: str, generation: str) -> None:
        self.publish_generation_calls.append((source_id, generation))
        self._active_generation[source_id] = generation

    def abort_generation(self, source_id: str, generation: str) -> None:
        self.abort_generation_calls.append((source_id, generation))
        self._docs.setdefault(source_id, {}).pop(generation, None)

    # -- writes -----------------------------------------------------------
    def upsert_file(self, file_record: FileRecord) -> None:
        return None

    def delete_file(self, source_id: str, file_id: str) -> None:
        return None

    def publish_code(self, prepared_code: PreparedCode) -> None:
        self.publish_code_calls += 1
        if (
            self._fail_on_nth_publish_code is not None
            and self.publish_code_calls == self._fail_on_nth_publish_code
        ):
            raise RuntimeError("simulated publish_code failure")
        generation = str(prepared_code.generation)
        bucket = self._docs.setdefault(prepared_code.source_id, {}).setdefault(generation, {})
        bucket[prepared_code.file_id] = {
            "entities": len(prepared_code.entities),
            "generation": generation,
        }

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        generation = str(prepared_document.generation)
        bucket = self._docs.setdefault(prepared_document.source_id, {}).setdefault(generation, {})
        bucket[prepared_document.file_id] = {"generation": generation, "kind": "document"}

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

    def get_entities_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        return []

    def get_document_units_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        return []

    def count_stats(self) -> BackendStats:
        return BackendStats()

    def clear_source(self, source_id: str) -> None:
        return None

    # -- test-only "current state" reader --------------------------------
    def active_state(self, source_id: str) -> dict[str, Any]:
        """What a reader filtering to the active generation would see --
        mirrors ``OpenSearchKnowledgeBackend``'s documented generation-
        filtered read contract.
        """
        generation = self._active_generation.get(source_id, "0")
        return dict(self._docs.get(source_id, {}).get(generation, {}))


def _server_ctx(ragmonk_home: Path, backend: KnowledgeBackend) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    ctx._server_backend = backend
    return ctx


def _register_source(ctx: AppContext, tmp_path: Path, *, n_files: int = 4) -> str:
    source_dir = tmp_path / "src"
    source_dir.mkdir(exist_ok=True)
    for i in range(n_files):
        (source_dir / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    source = registry.add(str(source_dir))
    return source.id


def test_server_rebuild_success_publishes_generation_once(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    backend = _FakeGenerationBackend()
    ctx = _server_ctx(ragmonk_home, backend)
    try:
        source_id = _register_source(ctx, tmp_path)
        outcomes = run_rebuild(ctx, source_id=source_id)
    finally:
        ctx.close()

    assert len(outcomes) == 1
    assert outcomes[0].result.failed == 0
    assert backend.begin_calls == [source_id]
    assert backend.publish_generation_calls == [(source_id, "1")]
    assert backend.abort_generation_calls == []
    assert backend.publish_code_calls > 0
    # The active generation's state reflects everything this rebuild wrote.
    assert len(backend.active_state(source_id)) == backend.publish_code_calls


def test_server_rebuild_failure_aborts_and_preserves_previous_generation(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FakeGenerationBackend()
    ctx = _server_ctx(ragmonk_home, backend)
    try:
        source_id = _register_source(ctx, tmp_path, n_files=6)
        run_rebuild(ctx, source_id=source_id)
    finally:
        ctx.close()
    assert backend.publish_generation_calls == [(source_id, "1")]
    previous_state = backend.active_state(source_id)
    assert previous_state  # a real "previous generation" exists to preserve

    # Second rebuild attempt: simulate a pass that publishes 2 files'
    # worth of content into the NEW generation and then hits a failure
    # partway through (its 3rd publish_code call) -- exercising exactly
    # the try/except/finally contract ``_rebuild_source_server`` must
    # guarantee, independent of the coordinator's own per-file
    # retry/backoff machinery (already covered elsewhere, out of this
    # phase's scope).
    import ragmonk.ops.rebuild as rebuild_module
    from ragmonk.backends.models import PreparedCode

    def _fake_run_source_pass(ctx: AppContext, source: Any, processors: Any, **kwargs: Any) -> Any:
        fake_backend = kwargs["backend"]
        generation = kwargs["force_generation"]
        for i in range(2):
            fake_backend.publish_code(
                PreparedCode(file_id=f"partial-{i}", source_id=source.id, generation=generation)
            )
        # The 3rd publish_code call of this attempt raises.
        fake_backend.publish_code(
            PreparedCode(file_id="partial-2", source_id=source.id, generation=generation)
        )
        raise AssertionError("unreachable: publish_code above must have raised")

    monkeypatch.setattr(rebuild_module, "run_source_pass", _fake_run_source_pass)
    backend._fail_on_nth_publish_code = backend.publish_code_calls + 3

    ctx2 = _server_ctx(ragmonk_home, backend)
    try:
        with pytest.raises(RuntimeError, match="simulated publish_code failure"):
            run_rebuild(ctx2, source_id=source_id)
    finally:
        ctx2.close()

    assert backend.begin_calls[-1] == source_id
    failed_generation = str(int(backend.publish_generation_calls[-1][1]) + 1)
    assert backend.abort_generation_calls == [(source_id, failed_generation)]
    # publish_generation was never called again after the failure.
    assert backend.publish_generation_calls == [(source_id, "1")]
    # The previously active generation's data is untouched.
    assert backend.active_state(source_id) == previous_state
    # And the failed attempt's 2 partial writes are gone too -- abort
    # cleaned up the whole incomplete generation, not just the doc that
    # raised.
    failed_gen_bucket = backend._docs.get(source_id, {}).get(failed_generation)
    assert not failed_gen_bucket


def test_retrying_a_failed_rebuild_is_safe_by_construction() -> None:
    """Not a resume-simulation test: a retried server-mode rebuild calls
    ``begin_generation`` again, gets a brand-new generation id (the fake
    above increments from the *last published* generation, matching
    ``OpenSearchKnowledgeBackend.begin_generation``'s real
    current-plus-one logic), and republishes every file from scratch
    under that new id. Nothing from the aborted attempt survives
    (``abort_generation`` already deleted it), and nothing from the
    retry can collide with the previously published generation because
    every document's id is deterministic *per file*, not per attempt
    (P4/P5's ``opensearch_ids``/``elasticsearch_ids`` -- "re-publishing
    the same file is an idempotent overwrite, not a duplicate"). So a
    retry is exactly a fresh, independent rebuild attempt: no separate
    resume/idempotency test is needed beyond the two above.
    """
