"""Completion plan Step 0 / F1-F4: server-mode end-to-end regression tests.

Each test drives the real CLI (or daemon/Admin-UI entrypoint) in
``storage.mode: server`` against an in-memory OpenSearch/Elasticsearch
fake (see ``_server_harness.py``) and proves searchable knowledge is
written to -- and read from -- the *server backend*, never local SQLite:

- F1: ``ragmonk init``-style server config -> ``source add`` ->
  ``ragmonk index`` -> ``ragmonk search`` returns results that come from
  the server backend.
- F2: the daemon's indexing pass publishes through the server backend and
  ``LocalKnowledgeBackend`` is never used as a writer.
- F3: the Admin UI background indexer, document list/detail and symbol
  listing all work in server mode.
- F4: callers/callees/impact/explore resolve real neighbor entities/files
  and include documentation evidence in server mode.

Before the completion fix every one of these failed (``ragmonk index``
wrote to local SQLite; UI services raised ``LocalStorageModeRequiredError``;
graph resolution returned ``neighbor_entity=None``; impact/explore
skipped document links).
"""

from __future__ import annotations

import json
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


@pytest.fixture
def forbid_local_writer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fails the test if any ``LocalKnowledgeBackend`` publish method is
    called -- in server mode local SQLite must never be the searchable-
    knowledge writer.
    """
    from ragmonk.backends.local import LocalKnowledgeBackend

    calls: list[str] = []
    for name in (
        "publish_code",
        "publish_document",
        "publish_embeddings",
        "publish_links",
        "upsert_file",
    ):

        def _boom(self: Any, *args: Any, _name: str = name, **kwargs: Any) -> Any:
            calls.append(_name)
            raise AssertionError(f"LocalKnowledgeBackend.{_name} used in server mode")

        monkeypatch.setattr(LocalKnowledgeBackend, name, _boom)
    return calls


def _index_and_source(
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
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output
    return fake, source_id, project


def test_cli_index_then_search_uses_server_backend(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
    forbid_local_writer: list[str],
) -> None:
    fake, source_id, _ = _index_and_source(ragmonk_home, tmp_path, monkeypatch, runner, engine)

    stored = docs(fake)
    entity_names = {d.get("name") for d in stored if d.get("doc_kind") == "entity"}
    assert "compute_invoice_total" in entity_names
    assert any(d.get("doc_kind") == "file" for d in stored)
    # first publication ran inside a generation that was then published
    markers = [d for d in stored if d.get("doc_kind") == "generation_marker"]
    assert markers and markers[0]["active_generation"] != "0"
    assert forbid_local_writer == []

    # The local per-project SQLite holds no searchable knowledge.
    with AppContext.bootstrap() as ctx:
        from ragmonk.core import paths
        from ragmonk.sources.registry import SourceRegistry

        source = SourceRegistry(ctx.sources_conn, home=ctx.home).get(source_id)
        conn = ctx.project_conn(
            paths.project_id_for_path(Path(source.path)), control_plane=True
        )
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0  # control plane

    result = runner.invoke(app, ["search", "compute_invoice_total", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = payload["data"]["results"]
    assert rows, result.output
    assert any(r["path"].endswith("billing.py") for r in rows), rows

    # Removing every server document makes search empty -> results really
    # came from the server backend, not from any local copy.
    for index in fake.store.values():
        index.clear()
    result = runner.invoke(app, ["search", "compute_invoice_total", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["results"] == []


def test_incremental_modify_delete_rename_in_server_mode(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    fake, _source_id, project = _index_and_source(
        ragmonk_home, tmp_path, monkeypatch, runner, engine
    )

    def entity_names() -> set[str]:
        return {str(d.get("name")) for d in docs(fake) if d.get("doc_kind") == "entity"}

    def file_paths() -> set[str]:
        return {
            Path(str(d.get("path"))).name for d in docs(fake) if d.get("doc_kind") == "file"
        }

    # add + modify
    (project / "extra.py").write_text("def brand_new_helper():\n    return 1\n")
    (project / "billing.py").write_text(
        "def compute_invoice_total(amount):\n    return amount\n\n"
        "def renamed_tax(amount):\n    return amount\n"
    )
    assert runner.invoke(app, ["index"]).exit_code == 0
    names = entity_names()
    assert {"brand_new_helper", "renamed_tax"} <= names
    assert "apply_tax" not in names

    # rename
    (project / "extra.py").rename(project / "moved_extra.py")
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert "moved_extra.py" in file_paths() and "extra.py" not in file_paths()
    assert "brand_new_helper" in entity_names()

    # delete
    (project / "moved_extra.py").unlink()
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert "brand_new_helper" not in entity_names()
    assert "moved_extra.py" not in file_paths()


def test_callers_callees_impact_explore_parity_in_server_mode(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    _index_and_source(ragmonk_home, tmp_path, monkeypatch, runner, engine)

    from ragmonk.code.graph import find_symbol_matches
    from ragmonk.core.models import RelationshipType
    from ragmonk.retrieval import graph as retrieval_graph

    with AppContext.bootstrap() as ctx:
        matches = find_symbol_matches(ctx, "compute_invoice_total")
        assert matches
        callees = retrieval_graph.resolved_outgoing(
            ctx, matches, relationship_types=(RelationshipType.CALLS,)
        )
        resolved = [e for e in callees if e.neighbor_entity is not None]
        assert resolved, "server-mode callees must resolve neighbor entities (F4)"
        assert resolved[0].neighbor_entity is not None
        assert resolved[0].neighbor_entity.name == "apply_tax"
        assert resolved[0].neighbor_file is not None
        assert resolved[0].neighbor_file.path.endswith("billing.py")

        callers = retrieval_graph.resolved_incoming(
            ctx, matches, "compute_invoice_total", relationship_types=(RelationshipType.CALLS,)
        )
        caller_files = {e.neighbor_file.path for e in callers if e.neighbor_file is not None}
        assert any(p.endswith("test_billing.py") for p in caller_files)
        tests = retrieval_graph.find_tests_referencing(ctx, matches, "compute_invoice_total")
        assert tests, "server-mode tests signal must work once neighbor files resolve"

    impact = runner.invoke(app, ["impact", "compute_invoice_total", "--json"])
    assert impact.exit_code == 0, impact.output
    impact_payload = json.loads(impact.output)["data"]
    assert impact_payload["defined"]
    assert impact_payload["documentation"], "server-mode impact must include doc links (F4)"
    assert any("README.md" in d["path"] for d in impact_payload["documentation"])

    explore = runner.invoke(app, ["explore", "compute_invoice_total", "--json"])
    assert explore.exit_code == 0, explore.output
    assert "README.md" in explore.output

    for command in ("symbol", "callers", "callees", "references"):
        result = runner.invoke(app, [command, "compute_invoice_total"])
        assert result.exit_code == 0, (command, result.output)


def test_daemon_pass_publishes_to_server_backend(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
    forbid_local_writer: list[str],
) -> None:
    fake = install_server_mode(ragmonk_home, monkeypatch, engine)
    project = tmp_path / "proj"
    write_project(project)
    source_id = add_source(runner, project)

    from ragmonk.service.daemon import Daemon

    with AppContext.bootstrap() as ctx:
        daemon = Daemon(ctx)
        daemon._run_pass(source_id)  # noqa: SLF001 - the daemon's real per-source pass

    names = {d.get("name") for d in docs(fake) if d.get("doc_kind") == "entity"}
    assert "compute_invoice_total" in names
    assert forbid_local_writer == []


def test_admin_ui_indexing_documents_and_symbols_in_server_mode(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
    forbid_local_writer: list[str],
) -> None:
    fake = install_server_mode(ragmonk_home, monkeypatch, engine)
    project = tmp_path / "proj"
    write_project(project)
    add_source(runner, project)

    from ragmonk.service import document_service, index_service, knowledge_service

    indexer = index_service.BackgroundIndexer()
    indexer._run(source_id=None, rebuild=False, fresh=False)  # noqa: SLF001 - synchronous run
    summary = indexer.snapshot()["last_summary"]
    assert summary and "error" not in summary, summary
    assert any(d.get("doc_kind") == "entity" for d in docs(fake))
    assert forbid_local_writer == []

    with AppContext.bootstrap() as ctx:
        listing = document_service.list_documents(ctx)
        assert listing["total"] >= 1
        readme = next(d for d in listing["documents"] if d["name"] == "README.md")
        detail = document_service.document_detail(ctx, readme["source_id"], readme["id"])
        assert detail["chunks"], "server-mode document detail must show chunks"
        assert any("compute_invoice_total" in c["text"] for c in detail["chunks"])

        symbols = knowledge_service.list_symbols(ctx)
        assert "compute_invoice_total" in {s["name"] for s in symbols}
        searched = knowledge_service.list_symbols(ctx, query="apply_tax")
        assert "apply_tax" in {s["name"] for s in searched}

        overview = index_service.indexing_overview(ctx)
        assert overview["counts"]
        assert index_service.failed_files(ctx) == []

        impact = knowledge_service.impact(ctx, "compute_invoice_total")
        assert impact["documentation"]
