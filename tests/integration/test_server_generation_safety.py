"""Completion plan F6: generation isolation, with failure injection.

Runs the real CLI in server mode against the in-memory engine fakes
(both OpenSearch and Elasticsearch adapters) and proves:

- a rebuild that fails part-way -- either a per-file write failure (a
  bulk error during an "outage") or a raised, pass-level failure --
  keeps the previously published generation *fully* consistent and
  searchable (entities, file records, documents, chunks, links), and
  ``abort_generation`` removes every artifact of the aborted generation
  in every index;
- while a rebuild is in progress (before ``publish_generation``) readers
  see only the old generation -- never a mix of old and new;
- a successful rebuild atomically switches to the new generation and
  garbage-collects every older generation's artifacts;
- ``source remove`` (``clear_source``) deletes every generation of the
  source, including an in-progress one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.integration._server_harness import (
    ENGINES,
    add_source,
    docs,
    install_server_mode,
    write_project,
)
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core.lifecycle import AppContext


@pytest.fixture(params=ENGINES)
def engine(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _visible(ctx: AppContext, source_id: str) -> dict[str, Any]:
    """Everything a reader can observe for ``source_id`` via the backend's
    generation-filtered reads.
    """
    backend = ctx.backend()
    entities = backend.list_source_entities(source_id)
    documents = backend.list_documents(source_id=source_id)
    files = backend.list_files(source_id)
    units = backend.list_source_document_units(source_id)
    links = backend.get_links(entity_ids=[e.id for e in entities])
    return {
        "entities": sorted(e.qualified_name for e in entities),
        "files": sorted(Path(f.path).name for f in files),
        "documents": sorted(d.title for d in documents),
        "units": len(units),
        "links": sorted((lk.link_type, lk.resolver) for lk in links),
        "search": sorted(
            str(h.payload.get("name")) for h in backend.lexical_search("compute_invoice_total", 50)
        ),
    }


def _setup(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> tuple[Any, str, Path]:
    fake = install_server_mode(ragmonk_home, monkeypatch, engine)
    project = tmp_path / "proj"
    write_project(project)
    source_id = add_source(runner, project)
    assert runner.invoke(app, ["index"]).exit_code == 0
    return fake, source_id, project


def _active_generation(fake: Any) -> str:
    markers = [d for d in docs(fake) if d.get("doc_kind") == "generation_marker"]
    assert len(markers) == 1
    return str(markers[0]["active_generation"])


def _generations_present(fake: Any) -> set[str]:
    return {str(d["generation"]) for d in docs(fake) if d.get("generation") is not None}


def test_failed_rebuild_per_file_bulk_failure_keeps_previous_generation(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    fake, source_id, project = _setup(ragmonk_home, tmp_path, monkeypatch, runner, engine)
    with AppContext.bootstrap() as ctx:
        before = _visible(ctx, source_id)
    assert before["entities"] and before["links"] and before["units"]
    old_generation = _active_generation(fake)

    # Change content so a published rebuild would be observably different.
    (project / "billing.py").write_text(
        "def compute_invoice_total(amount):\n    return amount\n\n"
        "def only_in_new(x):\n    return x\n"
    )

    module = __import__(f"ragmonk.backends.{engine}", fromlist=["run_bulk_or_raise"])
    original = module.run_bulk_or_raise
    calls = {"n": 0}

    def _flaky(client: Any, actions: Any, config: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:
            from ragmonk.core.errors import DatabaseError

            raise DatabaseError("simulated bulk outage")
        return original(client, actions, config)

    monkeypatch.setattr(module, "run_bulk_or_raise", _flaky)
    result = runner.invoke(app, ["rebuild", "--source", source_id])
    assert result.exit_code != 0, result.output
    monkeypatch.setattr(module, "run_bulk_or_raise", original)

    assert _active_generation(fake) == old_generation
    assert _generations_present(fake) == {old_generation}, "aborted generation left artifacts"
    with AppContext.bootstrap() as ctx:
        assert _visible(ctx, source_id) == before


def test_failed_rebuild_raised_failure_keeps_previous_generation(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    fake, source_id, project = _setup(ragmonk_home, tmp_path, monkeypatch, runner, engine)
    with AppContext.bootstrap() as ctx:
        before = _visible(ctx, source_id)
    old_generation = _active_generation(fake)
    (project / "extra.py").write_text("def only_in_new():\n    return 1\n")

    def _boom(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("simulated linking crash")

    monkeypatch.setattr("ragmonk.indexing.runner.link_touched_files", _boom)
    result = runner.invoke(app, ["rebuild", "--source", source_id])
    assert result.exit_code != 0

    assert _active_generation(fake) == old_generation
    assert _generations_present(fake) == {old_generation}
    with AppContext.bootstrap() as ctx:
        assert _visible(ctx, source_id) == before


def test_rebuild_is_atomic_no_mixed_generation_observable(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    fake, source_id, project = _setup(ragmonk_home, tmp_path, monkeypatch, runner, engine)
    with AppContext.bootstrap() as ctx:
        before = _visible(ctx, source_id)
    old_generation = _active_generation(fake)
    (project / "billing.py").write_text(
        "def compute_invoice_total(amount):\n    return amount\n\n"
        "def only_in_new(x):\n    return x\n"
    )
    (project / "README.md").write_text("# Billing v2\n\nUse `compute_invoice_total` now.\n")

    module = __import__(f"ragmonk.backends.{engine}", fromlist=["x"])
    cls = (
        module.OpenSearchKnowledgeBackend
        if engine == "opensearch"
        else module.ElasticsearchKnowledgeBackend
    )
    original_publish = cls.publish_generation
    observed: dict[str, Any] = {}

    def _observing_publish(self: Any, sid: str, generation: str) -> None:
        # Just before the atomic switch: every new artifact is written,
        # yet readers must still see exactly the old generation.
        with AppContext.bootstrap() as ctx:
            observed["mid"] = _visible(ctx, sid)
        observed["generations_mid"] = _generations_present(fake)
        original_publish(self, sid, generation)

    monkeypatch.setattr(cls, "publish_generation", _observing_publish)
    result = runner.invoke(app, ["rebuild", "--source", source_id])
    assert result.exit_code == 0, result.output

    assert observed["mid"] == before
    assert len(observed["generations_mid"]) == 2  # old + fully written new
    new_generation = _active_generation(fake)
    assert new_generation != old_generation
    assert _generations_present(fake) == {new_generation}, "old generation not GC'd"
    with AppContext.bootstrap() as ctx:
        after = _visible(ctx, source_id)
    assert "billing.only_in_new" in after["entities"]
    assert "billing.apply_tax" not in after["entities"]
    assert after["files"] == before["files"]


def test_source_remove_clears_every_generation(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    fake, source_id, _project = _setup(ragmonk_home, tmp_path, monkeypatch, runner, engine)
    from ragmonk.backends.models import PreparedCode
    from ragmonk.core.models import Entity, EntityType

    with AppContext.bootstrap() as ctx:
        backend = ctx.backend()
        in_progress = backend.begin_generation(source_id)
        entity = Entity(
            id="orphan-e", source_id=source_id, file_id="orphan", kind=EntityType.FUNCTION,
            name="orphan", qualified_name="orphan", language="python", start_line=1,
            end_line=1, generation=int(in_progress), created_at="", updated_at="",
        )
        backend.publish_code(
            PreparedCode(
                file_id="orphan",
                source_id=source_id,
                generation=int(in_progress),
                entities=[entity],
            )
        )
    assert in_progress in _generations_present(fake)
    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert [d for d in docs(fake) if d.get("source_id") == source_id] == []
