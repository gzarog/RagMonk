"""Unit tests for ``retrieval/lexical.py``'s ranking: fixtures where
exact/qualified/FTS/path (and document title/heading) signals differ,
asserting the blueprint's stated priority order actually holds once
merged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ragpilot.core.config import RagpilotConfig
from ragpilot.core.lifecycle import AppContext
from ragpilot.core.models import (
    Document,
    DocumentFormat,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
)
from ragpilot.retrieval import lexical
from ragpilot.sources.registry import SourceRegistry
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo, entities_repo, files_repo
from ragpilot.storage.sqlite import connect, transaction


def _file(file_id: str, path: str, kind: FileKind) -> FileRecord:
    return FileRecord(
        id=file_id,
        source_id="s1",
        path=path,
        kind=kind,
        size=10,
        mtime=0.0,
        status=FileStatus.INDEXED,
        created_at="now",
        updated_at="now",
    )


def _entity(entity_id: str, name: str, qualified_name: str, file_id: str) -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=qualified_name,
        language="python",
        start_line=1,
        end_line=2,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def _collect(
    conn: sqlite3.Connection, query: str, *, limit: int = 25
) -> list[lexical.SearchResult]:
    return lexical._merge(
        lexical._search_entities(conn, "s1", query, limit)
        + lexical._search_documents(conn, "s1", query, limit, snippet_max_tokens=32)
        + lexical._search_paths(conn, "s1", query, limit)
    )


def test_entity_ranking_prefers_exact_over_qualified_over_fts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "/repo/dog.py", FileKind.CODE))
        with transaction(conn):
            # Contrived (a real qualified name is rarely dotted-equal to a
            # bare name) but isolates the tier rule itself: entity.name ==
            # query must outrank entity.qualified_name == query.
            entities_repo.insert(
                conn, _entity("e_exact", "Dog.bark", "pkg.Dog.bark_alias", "f1"), snippet="exact"
            )
            entities_repo.insert(
                conn, _entity("e_qualified", "bark", "Dog.bark", "f1"), snippet="qualified"
            )
            entities_repo.insert(
                conn,
                _entity("e_fts", "unrelated", "pkg.unrelated", "f1"),
                snippet="the dog does bark loudly",
            )

        results = _collect(conn, "Dog.bark")
        ids = [r.id for r in results if r.kind == "entity"]
        assert ids == ["e_exact", "e_qualified", "e_fts"]
        assert results[0].tier == lexical.RankTier.EXACT_SYMBOL
        qualified = [r for r in results if r.id == "e_qualified"][0]
        assert qualified.tier == lexical.RankTier.QUALIFIED_SYMBOL
        assert [r for r in results if r.id == "e_fts"][0].tier == lexical.RankTier.FTS
    finally:
        conn.close()


def test_document_title_match_beats_fts_beats_path(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        # Deliberately doesn't contain "Guide" in its path -- this test is
        # isolating the title-tier signal from the path-tier signal, and a
        # path substring hit here would blur the two.
        files_repo.insert(conn, _file("f_doc1", "/repo/docs/manual.md", FileKind.DOCUMENT))
        files_repo.insert(conn, _file("f_doc2", "/repo/docs/other.md", FileKind.DOCUMENT))
        files_repo.insert(conn, _file("f_path", "/repo/docs/Guide-notes.txt", FileKind.DOCUMENT))
        with transaction(conn):
            documents_repo.insert_document(
                conn,
                Document(
                    id="d_title",
                    source_id="s1",
                    file_id="f_doc1",
                    format=DocumentFormat.MARKDOWN,
                    title="Guide",
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            documents_repo.insert_document(
                conn,
                Document(
                    id="d_fts",
                    source_id="s1",
                    file_id="f_doc2",
                    format=DocumentFormat.MARKDOWN,
                    title="Other",
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p_fts",
                    document_id="d_fts",
                    file_id="f_doc2",
                    text="See the Guide for background.",
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Other",
            )

        results = _collect(conn, "Guide")
        by_key = {(r.kind, r.id): r for r in results}
        assert by_key[("document", "d_title")].tier == lexical.RankTier.TITLE_OR_HEADING
        assert by_key[("document", "p_fts")].tier == lexical.RankTier.FTS
        assert by_key[("path", "f_path")].tier == lexical.RankTier.PATH

        order = [r.tier for r in results]
        assert order == sorted(order)
        assert results[0].id == "d_title"
        assert results[-1].id == "f_path"
    finally:
        conn.close()


def test_alias_lookup_uses_indexed_column_not_full_scan(tmp_path: Path) -> None:
    """Blueprint section 7: ``Class.member``-shaped queries should match
    the indexed ``entities.alias`` column (``entities_repo.
    search_alias_projection``) rather than the previous full-corpus
    Python scan -- this exercises the same "Betsson.Sportsbook.
    SettlementService.Process" example from the blueprint itself.
    """
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "/repo/settlement.py", FileKind.CODE))
        with transaction(conn):
            entities_repo.insert(
                conn,
                _entity(
                    "e_alias",
                    "Process",
                    "Betsson.Sportsbook.SettlementService.Process",
                    "f1",
                ),
                snippet="def Process(self): ...",
            )
            # Too few segments to earn an alias -- must never match.
            entities_repo.insert(
                conn,
                _entity("e_short", "Process", "Ns.Process", "f1"),
                snippet="def Process(): ...",
            )

        results = _collect(conn, "SettlementService.Process")
        by_id = {r.id: r for r in results if r.kind == "entity"}
        assert by_id["e_alias"].tier == lexical.RankTier.ALIAS_SYMBOL
        # "e_short" still surfaces via the permissive FTS OR-query (its
        # name/snippet both contain "Process"), but must never be tagged
        # as an alias match -- only its indexed ``alias`` column decides
        # that, and "Ns.Process" has too few segments to earn one.
        if "e_short" in by_id:
            assert by_id["e_short"].tier != lexical.RankTier.ALIAS_SYMBOL
    finally:
        conn.close()


def test_path_search_matches_by_token_and_falls_back_to_substring(tmp_path: Path) -> None:
    """Blueprint section 11: a filename token (indexed via ``path_fts``)
    and a mid-word fragment (only found via the ``LIKE`` fallback) must
    both still resolve to the same file.
    """
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "src/ragpilot/retrieval/vectorstore.py", FileKind.CODE))

        token_hits = files_repo.search_path_projection(conn, "vectorstore")
        assert [f.id for f in token_hits] == ["f1"]

        fragment_hits = files_repo.search_path_projection(conn, "ectorstore")
        assert [f.id for f in fragment_hits] == ["f1"]
    finally:
        conn.close()


def test_search_with_timings_caches_and_invalidates_on_external_write(
    ragpilot_home: Path, tmp_path: Path
) -> None:
    """Blueprint section 23: a repeated query against an unchanged
    project is served from cache (reported as a ``cache_hit`` stage),
    and a write from a *different* connection (a reindex, in practice)
    invalidates it automatically -- no explicit cache-clear call needed.
    """
    ctx = AppContext.bootstrap(home=ragpilot_home, cwd=tmp_path, cli_overrides={})
    try:
        project_root = tmp_path / "proj"
        project_root.mkdir()
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.add(str(project_root))
        from ragpilot.core import paths

        project_id = paths.project_id_for_path(project_root)
        conn = ctx.project_conn(project_id)
        files_repo.insert(conn, _file("f1", "pkg/dog.py", FileKind.CODE))
        with transaction(conn):
            entities_repo.insert(
                conn, _entity("e1", "Dog", "pkg.Dog", "f1"), snippet="class Dog: ..."
            )

        first = lexical.search_with_timings(ctx, "Dog")
        assert "e1" in [r.id for r in first.results]
        assert {t.name for t in first.timings} >= {"entities", "documents", "paths", "merge"}

        second = lexical.search_with_timings(ctx, "Dog")
        assert [r.id for r in second.results] == [r.id for r in first.results]
        assert [t.name for t in second.timings] == ["cache_hit"]

        # A write from a separate connection (mirrors a real reindex,
        # which always opens its own connection) bumps PRAGMA
        # data_version as observed by ``conn`` -- the cache key changes,
        # so the next search recomputes rather than serving stale data.
        other_conn = connect(paths.project_db_path(project_id, ctx.home))
        try:
            with transaction(other_conn):
                entities_repo.insert(
                    other_conn, _entity("e2", "Cat", "pkg.Cat", "f1"), snippet="class Cat: ..."
                )
        finally:
            other_conn.close()

        third = lexical.search_with_timings(ctx, "Dog")
        assert [t.name for t in third.timings] != ["cache_hit"]
    finally:
        ctx.close()


def test_search_with_timings_respects_cache_disabled(ragpilot_home: Path, tmp_path: Path) -> None:
    ctx = AppContext.bootstrap(
        home=ragpilot_home, cwd=tmp_path, cli_overrides={"search": {"cache": {"enabled": False}}}
    )
    try:
        assert isinstance(ctx.config, RagpilotConfig)
        project_root = tmp_path / "proj"
        project_root.mkdir()
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        registry.add(str(project_root))
        from ragpilot.core import paths

        project_id = paths.project_id_for_path(project_root)
        conn = ctx.project_conn(project_id)
        files_repo.insert(conn, _file("f1", "pkg/dog.py", FileKind.CODE))
        with transaction(conn):
            entities_repo.insert(
                conn, _entity("e1", "Dog", "pkg.Dog", "f1"), snippet="class Dog: ..."
            )

        first = lexical.search_with_timings(ctx, "Dog")
        second = lexical.search_with_timings(ctx, "Dog")
        assert [t.name for t in second.timings] != ["cache_hit"]
        assert [r.id for r in first.results] == [r.id for r in second.results]
    finally:
        ctx.close()


def test_merge_deduplicates_to_best_tier(tmp_path: Path) -> None:
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f1", "/repo/settlement.py", FileKind.CODE))
        with transaction(conn):
            entities_repo.insert(
                conn,
                _entity("e1", "SettlementService", "pkg.SettlementService", "f1"),
                snippet="class SettlementService: settlement logic",
            )

        results = lexical._merge(lexical._search_entities(conn, "s1", "SettlementService", 25))
        matching = [r for r in results if r.id == "e1"]
        assert len(matching) == 1
        assert matching[0].tier == lexical.RankTier.EXACT_SYMBOL
    finally:
        conn.close()
