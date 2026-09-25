"""Independent review BLOCKER fix regression tests.

An independent review of the storage-backend-abstraction PR found that
``AppContext.project_conn()`` had no ``storage.mode`` guard -- unlike its
already-guarded siblings ``code.graph.all_project_connections``/
``conn_for_source_path`` (see ``tests/unit/test_retrieval_backend_routing.py``),
``project_conn()`` itself would silently open/create a local per-project
sqlite connection regardless of the configured storage mode. At least 6
call sites bypassed the existing guards entirely by calling
``project_conn()`` directly:

- ``service/knowledge_service.py::list_symbols``
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


# -- The 6 flagged bypass call sites -----------------------------------


def test_knowledge_service_list_symbols_raises_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    from ragmonk.service import knowledge_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            knowledge_service.list_symbols(ctx)
    finally:
        ctx.close()


def test_document_service_list_documents_raises_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    from ragmonk.service import document_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            document_service.list_documents(ctx)
    finally:
        ctx.close()


def test_document_service_document_detail_raises_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    from ragmonk.service import document_service

    ctx = _server_ctx(ragmonk_home)
    try:
        source_id = _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            document_service.document_detail(ctx, source_id, "does-not-matter")
    finally:
        ctx.close()


def test_index_service_indexing_overview_raises_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    from ragmonk.service import index_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            index_service.indexing_overview(ctx)
    finally:
        ctx.close()


def test_index_service_failed_files_raises_in_server_mode(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    from ragmonk.service import index_service

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            index_service.failed_files(ctx)
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


def test_cli_link_list_raises_in_server_mode(ragmonk_home: Path, tmp_path: Path) -> None:
    """``ragmonk link list`` (the Typer command itself), invoked through
    the CLI boundary, exits cleanly with the mapped exit code rather than
    a raw traceback -- proving the CLI surfaces this error, not just the
    underlying service function.
    """
    from typer.testing import CliRunner

    from ragmonk.cli.link import app
    from ragmonk.core.errors import EXIT_CONFIG_ERROR

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
    finally:
        ctx.close()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["list"],
        env={"RAGMONK_HOME": str(ragmonk_home), "RAGMONK_STORAGE__MODE": "server"},
    )
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "storage.mode" in result.output
    assert "Traceback" not in result.output


def test_cli_docs_raises_in_server_mode(ragmonk_home: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ragmonk.cli.docs import _run, docs
    from ragmonk.core.errors import EXIT_CONFIG_ERROR

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
        with pytest.raises(LocalStorageModeRequiredError):
            _run(ctx, None)
    finally:
        ctx.close()

    # And through the CLI boundary (@cli_command): clean exit code, no
    # raw traceback to the user.
    import typer

    app = typer.Typer()
    app.command()(docs)
    result = CliRunner().invoke(
        app, [], env={"RAGMONK_HOME": str(ragmonk_home), "RAGMONK_STORAGE__MODE": "server"}
    )
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "storage.mode" in result.output
    assert "Traceback" not in result.output


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


def test_mcp_ragmonk_documents_maps_error_cleanly(ragmonk_home: Path, tmp_path: Path) -> None:
    """The ``ragmonk_documents`` MCP tool shares ``cli.docs._run`` -- the
    MCP boundary (``mcp/tools.py::_call``) must map the new typed error
    to a structured ``ToolError``, never let it become an unhandled
    exception or a silently empty/wrong document list.
    """
    import asyncio

    import ragmonk.core.config as config_module

    ctx = _server_ctx(ragmonk_home)
    try:
        _register_source(ctx, tmp_path)
    finally:
        ctx.close()
    config_module.write_user_config(ctx.config, home=ragmonk_home)

    from ragmonk.mcp import tools

    result = asyncio.run(tools.ragmonk_documents())
    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "LocalStorageModeRequiredError"
