"""Functional coverage for the server-aware ``ragmonk docs``/``ragmonk
link`` CLI commands and the ``ragmonk_documents`` MCP tool (completion
plan: close the "still local-only" gap flagged in
``tests/unit/test_project_conn_server_mode_guard.py``'s original
regression tests).

Uses the same fake OpenSearch engine
(``tests/unit/_fake_opensearch.FakeOpenSearch``) the read-contract tests
use, so no real cluster or ``opensearch-py`` install is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends.models import (
    FileRecord,
    LinkCandidate,
    PreparedCode,
    PreparedDocument,
    PreparedLinks,
)
from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import (
    Confidence,
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    RelationshipType,
)
from ragmonk.documents.chunker import Chunk
from ragmonk.sources.registry import SourceRegistry


def _server_ctx(ragmonk_home: Path) -> AppContext:
    ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    assert ctx.config.storage.mode == "server"
    return ctx


def _register_source(ctx: AppContext, tmp_path: Path, name: str = "proj") -> Any:
    source_dir = tmp_path / name
    source_dir.mkdir(exist_ok=True)
    (source_dir / "a.py").write_text("def fn():\n    pass\n")
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    return registry.add(str(source_dir))


def _guard_no_project_conn(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test the moment anything calls ``project_conn`` for a
    genuine (non-control-plane) knowledge read/write -- the exact
    regression this task closes.
    """
    original = AppContext.project_conn

    def _guarded(self: AppContext, project_id: str, *, control_plane: bool = False) -> Any:
        assert control_plane, "server-mode docs/link call site read local sqlite for knowledge"
        return original(self, project_id, control_plane=control_plane)

    monkeypatch.setattr(AppContext, "project_conn", _guarded)


def _seed_one_document(ctx: AppContext, source_id: str, *, suffix: str = "1") -> None:
    backend = ctx.backend()
    file_c, file_d = f"fc{suffix}", f"fd{suffix}"
    entity_id, document_id, unit_id = f"e{suffix}", f"d{suffix}", f"u{suffix}"
    backend.upsert_files(
        [
            FileRecord(
                file_id=file_c, source_id=source_id, path="a.py", content_hash="h",
                metadata={"kind": "code", "status": "done"},
            ),
            FileRecord(
                file_id=file_d, source_id=source_id, path="docs/report.md", content_hash="h",
                metadata={"kind": "document", "status": "done"},
            ),
        ]
    )
    backend.publish_code(
        PreparedCode(
            file_id=file_c,
            source_id=source_id,
            entities=[
                Entity(
                    id=entity_id, source_id=source_id, file_id=file_c, kind=EntityType.FUNCTION,
                    name="fn", qualified_name="a.fn", language="python", signature="def fn()",
                    start_line=1, end_line=2, generation=0, created_at="t", updated_at="t",
                )
            ],
        )
    )
    document = Document(
        id=document_id, source_id=source_id, file_id=file_d, format=DocumentFormat.MARKDOWN,
        title="Report", section_count=1, generation=0, created_at="t", updated_at="t",
    )
    chunk = Chunk(
        kind="paragraph", text="about fn", heading_level=None, heading_path=("Intro",),
        parent_index=None, page_start=1, page_end=1, contextual_text="ctx", search_text="about fn",
    )
    backend.publish_document(
        PreparedDocument(
            file_id=file_d, source_id=source_id, document=document, chunk_ids=[unit_id],
            chunks=[chunk], doc_title="Report",
        )
    )


# -- Task A/B: ``ragmonk docs`` / ``ragmonk_documents`` ----------------------


def test_docs_server_mode_lists_document_matching_local_schema(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.cli.docs import _run

    ctx = _server_ctx(ragmonk_home)
    backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=FakeOpenSearch())
    backend.ensure_schema()
    ctx._server_backend = backend  # noqa: SLF001 - inject the fake engine
    _guard_no_project_conn(ctx, monkeypatch)
    try:
        source = _register_source(ctx, tmp_path)
        _seed_one_document(ctx, source.id)

        rows = _run(ctx, None)
        assert len(rows) == 1
        row = rows[0]
        assert row["path"] == "docs/report.md"
        assert row["status"] == "done"
        assert row["format"] == DocumentFormat.MARKDOWN.value
        assert row["title"] == "Report"
        assert row["section_count"] == 1

        # ``--source`` filtering still works.
        assert _run(ctx, source.id) == rows
    finally:
        ctx.close()


def test_docs_server_mode_local_output_unchanged(ragmonk_home: Path, tmp_path: Path) -> None:
    """Local mode's ``_run`` output is untouched by the server branch."""
    from ragmonk.cli.docs import _run

    ctx = AppContext.bootstrap()
    try:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source_dir = tmp_path / "proj"
        source_dir.mkdir()
        (source_dir / "a.py").write_text("x = 1\n")
        registry.add(str(source_dir))
        assert _run(ctx, None) == []
    finally:
        ctx.close()


def test_mcp_ragmonk_documents_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import ragmonk.backends.factory as factory_module
    import ragmonk.core.config as config_module

    ctx = _server_ctx(ragmonk_home)
    try:
        source = _register_source(ctx, tmp_path)
    finally:
        ctx.close()
    config_module.write_user_config(ctx.config, home=ragmonk_home)

    fake_client = FakeOpenSearch()

    def _fake_create_backend(config: Any, *, home: Any = None) -> Any:
        backend = OpenSearchKnowledgeBackend(config.server, client=fake_client)
        backend.ensure_schema()
        return backend

    monkeypatch.setattr(factory_module, "create_backend", _fake_create_backend)

    # Seed through a throwaway backend instance sharing the same fake
    # client store, then let the MCP tool build its own AppContext.
    seed_backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=fake_client)
    seed_backend.ensure_schema()
    seed_ctx = AppContext.bootstrap(cli_overrides={"storage": {"mode": "server"}})
    seed_ctx._server_backend = seed_backend  # noqa: SLF001
    _seed_one_document(seed_ctx, source.id)
    seed_ctx.close()

    from ragmonk.mcp import tools

    result = asyncio.run(tools.ragmonk_documents())
    assert result.ok is True
    assert len(result.documents) == 1
    assert result.documents[0].path == "docs/report.md"


# -- Task C: ``ragmonk link`` ------------------------------------------------


def test_link_add_list_remove_round_trip_server_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.cli import link as link_cli

    ctx = _server_ctx(ragmonk_home)
    backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=FakeOpenSearch())
    backend.ensure_schema()
    ctx._server_backend = backend  # noqa: SLF001
    _guard_no_project_conn(ctx, monkeypatch)
    try:
        source = _register_source(ctx, tmp_path)
        _seed_one_document(ctx, source.id)

        # add: resolves entity/document by name/path, writes an explicit
        # user link, idempotently (re-adding is a no-op, not a dup).
        candidate = LinkCandidate(
            entity_id="e1", document_id="d1", section_id=None,
            link_type=RelationshipType.DOCUMENTED_BY, resolver="user",
            confidence=Confidence.EXACT, evidence="",
        )
        created = backend.publish_links(
            PreparedLinks(source_id=source.id, candidates=[candidate], generation=None)
        )
        assert created == 1
        created_again = backend.publish_links(
            PreparedLinks(source_id=source.id, candidates=[candidate], generation=None)
        )
        assert created_again == 0

        # list
        rows = link_cli._list_links_server(ctx, None, None, None)
        assert len(rows) == 1
        link_id = rows[0]["link_id"]
        assert link_id
        assert rows[0]["entity"] == "a.fn"
        assert rows[0]["document_path"] == "docs/report.md"

        # remove: deletes exactly this link, never anything else.
        assert backend.remove_link(link_id) is True
        assert link_cli._list_links_server(ctx, None, None, None) == []
        assert backend.remove_link(link_id) is False  # already gone

        # sanity: removing a link never dangling-deleted the entity/document.
        assert backend.get_entities(["e1"])[0].name == "fn"
        assert backend.get_documents(["d1"])[0].title == "Report"
    finally:
        ctx.close()


def test_link_remove_never_touches_a_different_source_or_link(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """Two sources, each with their own manual link sharing the same
    entity/document/section natural key shape but different ids --
    removing one must never remove the other.
    """
    ctx = _server_ctx(ragmonk_home)
    backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=FakeOpenSearch())
    backend.ensure_schema()
    ctx._server_backend = backend  # noqa: SLF001
    try:
        source_a = _register_source(ctx, tmp_path, "proj-a")
        source_b = _register_source(ctx, tmp_path, "proj-b")
        _seed_one_document(ctx, source_a.id, suffix="A")
        _seed_one_document(ctx, source_b.id, suffix="B")

        candidate_a = LinkCandidate(
            entity_id="eA", document_id="dA", section_id=None,
            link_type=RelationshipType.DOCUMENTED_BY, resolver="user",
            confidence=Confidence.EXACT, evidence="",
        )
        candidate_b = LinkCandidate(
            entity_id="eB", document_id="dB", section_id=None,
            link_type=RelationshipType.DOCUMENTED_BY, resolver="user",
            confidence=Confidence.EXACT, evidence="",
        )
        backend.publish_links(PreparedLinks(source_id=source_a.id, candidates=[candidate_a]))
        backend.publish_links(PreparedLinks(source_id=source_b.id, candidates=[candidate_b]))

        links_a = backend.get_links(entity_ids=["eA", "eB"])
        assert len(links_a) == 2
        link_a = next(link for link in links_a if link.source_id == source_a.id)
        link_b = next(link for link in links_a if link.source_id == source_b.id)
        assert link_a.id != link_b.id

        assert backend.remove_link(link_a.id) is True
        remaining = backend.get_links(entity_ids=["eA", "eB"])
        assert [link.source_id for link in remaining] == [source_b.id]
    finally:
        ctx.close()


def test_rebuild_does_not_resurrect_an_explicitly_removed_manual_link(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """A manual link removed via ``ragmonk link remove`` must not
    reappear just because the source is later rebuilt (a fresh
    begin/publish generation of its code+documents, with no auto-linker
    pass re-adding it).
    """
    ctx = _server_ctx(ragmonk_home)
    backend = OpenSearchKnowledgeBackend(ctx.config.storage.server, client=FakeOpenSearch())
    backend.ensure_schema()
    ctx._server_backend = backend  # noqa: SLF001
    try:
        source = _register_source(ctx, tmp_path)
        _seed_one_document(ctx, source.id)
        candidate = LinkCandidate(
            entity_id="e1", document_id="d1", section_id=None,
            link_type=RelationshipType.DOCUMENTED_BY, resolver="user",
            confidence=Confidence.EXACT, evidence="",
        )
        backend.publish_links(PreparedLinks(source_id=source.id, candidates=[candidate]))
        links = backend.get_links(entity_ids=["e1"])
        assert len(links) == 1
        assert backend.remove_link(links[0].id) is True
        assert backend.get_links(entity_ids=["e1"]) == []

        # Rebuild: a fresh generation republishing the same file/entity/
        # document (but not the manual link), then published.
        generation = backend.begin_generation(source.id)
        backend.upsert_files(
            [
                FileRecord(
                    file_id="fc1", source_id=source.id, path="a.py", content_hash="h2",
                    generation=int(generation), metadata={"kind": "code", "status": "done"},
                ),
                FileRecord(
                    file_id="fd1", source_id=source.id, path="docs/report.md", content_hash="h2",
                    generation=int(generation), metadata={"kind": "document", "status": "done"},
                ),
            ]
        )
        backend.publish_code(
            PreparedCode(
                file_id="fc1", source_id=source.id, generation=int(generation),
                entities=[
                    Entity(
                        id="e1", source_id=source.id, file_id="fc1", kind=EntityType.FUNCTION,
                        name="fn", qualified_name="a.fn", language="python", signature="def fn()",
                        start_line=1, end_line=2, generation=int(generation), created_at="t",
                        updated_at="t",
                    )
                ],
            )
        )
        document = Document(
            id="d1", source_id=source.id, file_id="fd1", format=DocumentFormat.MARKDOWN,
            title="Report", section_count=1, generation=int(generation), created_at="t",
            updated_at="t",
        )
        chunk = Chunk(
            kind="paragraph", text="about fn", heading_level=None, heading_path=("Intro",),
            parent_index=None, page_start=1, page_end=1, contextual_text="ctx",
            search_text="about fn",
        )
        backend.publish_document(
            PreparedDocument(
                file_id="fd1", source_id=source.id, generation=int(generation), document=document,
                chunk_ids=["u1"], chunks=[chunk], doc_title="Report",
            )
        )
        backend.publish_generation(source.id, generation)

        # The rebuilt entity/document are visible again, but the removed
        # manual link is NOT resurrected.
        assert backend.get_entities(["e1"])[0].name == "fn"
        assert backend.get_links(entity_ids=["e1"]) == []
    finally:
        ctx.close()
