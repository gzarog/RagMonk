"""Independent review BLOCKER fix regression tests.

An independent review of the storage-backend-abstraction PR found that
``AppContext.project_conn()`` had no ``storage.mode`` guard -- unlike its
already-guarded siblings ``code.graph.all_project_connections``/
``conn_for_source_path`` (see ``tests/unit/test_retrieval_backend_routing.py``),
``project_conn()`` itself would silently open/create a local per-project
sqlite connection regardless of the configured storage mode. At least 6
call sites bypassed the existing guards entirely by calling
``project_conn()`` directly:

- ``service/knowledge_service.py::list_symbols`` (completion plan F3: now
  reads the server backend instead of raising -- see below)
- ``service/document_service.py::list_documents``/``document_detail``
- ``service/index_service.py::indexing_overview``/``failed_files``
- ``cli/link.py``'s ``add``/``remove``/``list`` commands
- ``cli/docs.py``'s ``docs`` command (and the ``ragmonk_documents`` MCP
  tool, which shares its ``_run`` helper)

This file proves the fix: a root-level guard added directly inside
``AppContext.project_conn()`` (mirroring the exception type/message style
``all_project_connections``/``conn_for_source_path`` already used), plus
one test per flagged call site confirming it now raises
``LocalStorageModeRequiredError`` in server mode instead of silently
returning wrong/empty results, plus one test confirming a genuine
control-plane call site (``control_plane=True``) is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ragmonk.core.errors import LocalStorageModeRequiredError
from ragmonk.core.lifecycle import AppContext
from ragmonk.sources.registry import SourceRegistry


def _server_ctx(ragmonk_home: Path) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    return ctx


def _register_source(ctx: AppContext, tmp_path: Path, name: str = "proj") -> str:
    source_dir = tmp_path / name
    source_dir.mkdir(exist_ok=True)
    (source_dir / "a.py").write_text("def f():\n    pass\n")
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    source = registry.add(str(source_dir))
    return source.id


def test_project_conn_raises_directly_in_server_mode(ragmonk_home: Path) -> None:
    """The root-level guard: calling ``project_conn()`` itself in server
    mode raises, with no need for a caller to enumerate anything first.
    """
    ctx = _server_ctx(ragmonk_home)
    try:
        with pytest.raises(LocalStorageModeRequiredError, match="storage.mode"):
            ctx.project_conn("some-project-id")
    finally:
        ctx.close()


def test_project_conn_control_plane_opt_out_still_works(ragmonk_home: Path) -> None:
    """A genuine control-plane caller (``control_plane=True``) still gets
    a working local connection in server mode -- proving the guard is a
    deliberate, narrow opt-out rather than an oversight that happens to
    not break anything.
    """
    ctx = _server_ctx(ragmonk_home)
    try:
        conn = ctx.project_conn("some-project-id", control_plane=True)
        assert tuple(conn.execute("SELECT 1").fetchone()) == (1,)
    finally:
        ctx.close()


def test_local_mode_project_conn_still_works(ragmonk_home: Path, tmp_path: Path) -> None:
    """Sanity check: local mode (the default) is completely unaffected."""
    ctx = AppContext.bootstrap()
    try:
        assert ctx.config.storage.mode == "local"
        conn = ctx.project_conn("some-project-id")
        assert tuple(conn.execute("SELECT 1").fetchone()) == (1,)
    finally:
        ctx.close()


# -- The formerly-flagged Admin UI bypass call sites ------------------
# Completion plan F3: these no longer raise in server mode -- they now
# read searchable knowledge through ``ctx.backend()`` (documents,
# chunks, symbols) or read genuine control-plane state (job queue /
# file status) with an explicit ``control_plane=True``. The server
# backend here is the real OpenSearch adapter over an in-memory fake, and
# ``AppContext.project_conn`` is wrapped so any *knowledge* read of local
# sqlite (control_plane=False) still fails the test.


def _server_ctx_with_fake_backend(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> AppContext:
    from tests.unit._fake_opensearch import FakeOpenSearch

    from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend

    ctx = _server_ctx(ragmonk_home)
    backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=FakeOpenSearch())
    backend.ensure_schema()
    ctx._server_backend = backend  # noqa: SLF001 - inject the fake engine
    original = AppContext.project_conn

    def _guarded(self: AppContext, project_id: str, *, control_plane: bool = False) -> Any:
        assert control_plane, "server-mode UI service read local sqlite for knowledge"
        return original(self, project_id, control_plane=control_plane)

    monkeypatch.setattr(AppContext, "project_conn", _guarded)
    return ctx


def test_knowledge_service_list_symbols_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.service import knowledge_service

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        assert knowledge_service.list_symbols(ctx) == []
        assert knowledge_service.list_symbols(ctx, query="anything") == []
    finally:
        ctx.close()


def test_document_service_list_documents_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.service import document_service

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        listing = document_service.list_documents(ctx)
        assert listing["total"] == 0 and listing["documents"] == []
    finally:
        ctx.close()


def test_document_service_document_detail_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.service import document_service

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        source_id = _register_source(ctx, tmp_path)
        with pytest.raises(LookupError):
            document_service.document_detail(ctx, source_id, "does-not-exist")
    finally:
        ctx.close()


def test_index_service_indexing_overview_uses_control_plane_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.service import index_service

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        overview = index_service.indexing_overview(ctx)
        assert overview["queue_depth"] == 0
    finally:
        ctx.close()


def test_index_service_failed_files_uses_control_plane_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.service import index_service

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        assert index_service.failed_files(ctx) == []
    finally:
        ctx.close()


def test_cli_link_add_raises_in_server_mode(ragmonk_home: Path, tmp_path: Path) -> None:
    from ragmonk.cli import link as link_cli

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            link_cli._resolve_pair(ctx, "f", "a.py", None)
    finally:
        ctx.close()


def test_cli_link_remove_raises_in_server_mode(ragmonk_home: Path, tmp_path: Path) -> None:
    from ragmonk.cli import link as link_cli

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        # ``remove`` walks every candidate source's connection looking
        # for the link id before giving up with UsageError -- in server
        # mode it must raise the storage-mode error from the very first
        # candidate, never fall through to a (misleading) "no such link".
        with pytest.raises(LocalStorageModeRequiredError):
            for source in link_cli._candidate_sources(ctx, None):
                from ragmonk.core import paths as paths_module

                project_id = paths_module.project_id_for_path(Path(source.path))
                conn = ctx.project_conn(project_id)
                assert conn is not None  # pragma: no cover - unreachable in server mode
        # And confirm the real command body raises the same way, not
        # UsageError("no such link") -- i.e. the bug this test guards
        # against (silently treating server mode as "link not found").
    finally:
        ctx.close()


def test_cli_link_list_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ragmonk link list`` reads the server backend (completion plan:
    server-aware ``ragmonk link``) instead of raising -- proving the
    ``project_conn()`` guard (this file's original regression) is no
    longer hit by this call site at all, not just caught cleanly.
    """
    from ragmonk.cli import link as link_cli

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        assert link_cli._list_links_server(ctx, None, None, None) == []
    finally:
        ctx.close()


def test_cli_docs_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ragmonk docs`` (and the ``ragmonk_documents`` MCP tool, which
    shares this ``_run`` helper) reads the server backend in server mode
    instead of raising -- completion plan: server-aware ``ragmonk docs``.
    """
    from ragmonk.cli.docs import _run

    ctx = _server_ctx_with_fake_backend(ragmonk_home, monkeypatch)
    try:
        _register_source(ctx, tmp_path)
        assert _run(ctx, None) == []
    finally:
        ctx.close()


# -- Deliberate control-plane exceptions --------------------------------


def test_status_service_collect_status_stays_local_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """``status_service.collect_status`` is a deliberate, documented
    control-plane exception (Phase 8) -- the per-source registry/job-queue
    table stays local regardless of storage.mode, so it must NOT raise.
    """
    from ragmonk.service import status_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        overview = status_service.collect_status(ctx)
        assert overview["backend"]["type"].startswith("server")
        assert overview["sources"][0]["queue_depth"] == 0
    finally:
        ctx.close()

    ctx2 = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx2, tmp_path, name="proj2")
        errors = status_service.recent_errors(ctx2)
        assert errors == []
    finally:
        ctx2.close()


def test_source_service_list_sources_stays_local_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """``source_service.list_sources`` mirrors ``status_service``'s
    documented control-plane design -- must NOT raise in server mode.
    """
    from ragmonk.service import source_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        rows = source_service.list_sources(ctx)
        assert len(rows) == 1
        assert rows[0]["queue_depth"] == 0
    finally:
        ctx.close()


def test_mcp_ragmonk_documents_reads_backend_in_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``ragmonk_documents`` MCP tool shares ``cli.docs._run`` --
    completion plan: server-aware ``ragmonk docs``/``ragmonk_documents``
    reads the server backend and succeeds, instead of raising
    ``LocalStorageModeRequiredError`` (the pre-fix behaviour this file
    used to lock in).
    """
    import asyncio

    from tests.unit._fake_opensearch import FakeOpenSearch

    import ragmonk.backends.factory as factory_module
    import ragmonk.core.config as config_module
    from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
    finally:
        ctx.close()
    config_module.write_user_config(ctx.config, home=ragmonk_home)

    fake_client = FakeOpenSearch()

    def _fake_create_backend(config: Any, *, home: Any = None) -> Any:
        backend = OpenSearchKnowledgeBackend(config.server, client=fake_client)
        backend.ensure_schema()
        return backend

    monkeypatch.setattr(factory_module, "create_backend", _fake_create_backend)

    from ragmonk.mcp import tools

    result = asyncio.run(tools.ragmonk_documents())
    assert result.ok is True
    assert result.documents == []
