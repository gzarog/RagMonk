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


def test_build_query_plan_single_token_collapses_tiers() -> None:
    """Phase 6: a single-word/identifier query must behave exactly like
    today -- Tier A (phrase) and Tier D (OR fallback) collapse to the
    same one-token expression, and Tier B/C (all-terms, prefix) simply
    don't exist for it.
    """
    plan = lexical._build_query_plan("Dog")
    assert plan is not None
    assert plan.all_terms is None
    assert plan.prefix is None
    assert plan.phrase.expression == plan.fallback.expression == '"Dog"'
    assert plan.phrase.tier == lexical.LexicalTier.PHRASE
    assert plan.variants() == [plan.phrase, plan.fallback]


def test_build_query_plan_multi_word_builds_distinct_ordered_tiers() -> None:
    plan = lexical._build_query_plan("delayed settlement provider")
    assert plan is not None
    assert plan.phrase.expression == '"delayed settlement provider"'
    assert plan.all_terms is not None
    assert plan.all_terms.expression == '"delayed" AND "settlement" AND "provider"'
    assert plan.fallback.expression == '"delayed" OR "settlement" OR "provider"'
    # Every token here is long enough (>= 4 chars) to be worth a
    # selective prefix fallback.
    assert plan.prefix is not None
    assert plan.prefix.expression == "delayed* AND settlement* AND provider*"
    assert [v.tier for v in plan.variants()] == [
        lexical.LexicalTier.PHRASE,
        lexical.LexicalTier.ALL_TERMS,
        lexical.LexicalTier.PREFIX,
        lexical.LexicalTier.OR_FALLBACK,
    ]


def test_build_query_plan_skips_prefix_tier_for_only_short_tokens() -> None:
    """Tier C is used selectively -- short tokens (the overwhelming
    majority-noise case, e.g. "a"/"to"/"be") never get prefix-wildcarded,
    so a query made entirely of them earns no (redundant, noisy) prefix
    tier at all.
    """
    plan = lexical._build_query_plan("a to be")
    assert plan is not None
    assert plan.prefix is None


def test_run_query_plan_stops_once_a_tier_has_enough_results() -> None:
    plan = lexical._build_query_plan("delayed settlement provider")
    assert plan is not None
    calls: list[str] = []

    def execute(expression: str) -> list[str]:
        calls.append(expression)
        return ["hit-1"]

    tagged = lexical._run_query_plan(plan, 1, execute, lambda row: row)
    assert calls == [plan.phrase.expression]
    assert tagged == [("hit-1", lexical.LexicalTier.PHRASE)]


def test_run_query_plan_falls_through_every_tier_when_short_on_results() -> None:
    plan = lexical._build_query_plan("delayed settlement provider")
    assert plan is not None
    calls: list[str] = []

    def execute(expression: str) -> list[str]:
        calls.append(expression)
        return []

    tagged = lexical._run_query_plan(plan, 5, execute, lambda row: row)
    assert calls == [v.expression for v in plan.variants()]
    assert tagged == []


def test_run_query_plan_dedupes_identical_expressions_for_single_token() -> None:
    """Regression guard for the "no slowdown on single-token queries"
    requirement: every tier collapsing to the same expression must
    result in exactly one executed query, matching pre-Phase-6 behavior.
    """
    plan = lexical._build_query_plan("Dog")
    assert plan is not None
    calls: list[str] = []

    def execute(expression: str) -> list[str]:
        calls.append(expression)
        return ["hit"]

    tagged = lexical._run_query_plan(plan, 10, execute, lambda row: row)
    assert calls == ['"Dog"']
    assert tagged == [("hit", lexical.LexicalTier.PHRASE)]


def test_multi_word_query_ranks_phrase_match_above_or_only_noise(tmp_path: Path) -> None:
    """Phase 6's headline acceptance criterion: for a multi-word query,
    a document only the permissive OR-fallback tier finds (today's only
    signal) must never outrank a document the exact-phrase tier actually
    matched, even though both still show up in the same ``RankTier.FTS``
    bucket.
    """
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        files_repo.insert(conn, _file("f_relevant", "/repo/docs/relevant.md", FileKind.DOCUMENT))
        files_repo.insert(conn, _file("f_noise", "/repo/docs/noise.md", FileKind.DOCUMENT))
        with transaction(conn):
            documents_repo.insert_document(
                conn,
                Document(
                    id="d_relevant",
                    source_id="s1",
                    file_id="f_relevant",
                    format=DocumentFormat.MARKDOWN,
                    title="Provider Guide",
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p_relevant",
                    document_id="d_relevant",
                    file_id="f_relevant",
                    text="The delayed settlement provider retries automatically.",
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Provider Guide",
            )
            documents_repo.insert_document(
                conn,
                Document(
                    id="d_noise",
                    source_id="s1",
                    file_id="f_noise",
                    format=DocumentFormat.MARKDOWN,
                    title="Onboarding",
                    generation=1,
                    created_at="now",
                    updated_at="now",
                ),
            )
            # Only shares one of the three query tokens ("provider") --
            # today's permissive OR-of-every-token query alone can't tell
            # this apart from the genuinely relevant paragraph above.
            documents_repo.insert_paragraph(
                conn,
                Paragraph(
                    id="p_noise",
                    document_id="d_noise",
                    file_id="f_noise",
                    text="Our new provider onboarding process starts soon.",
                    order_index=0,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Onboarding",
            )

        results = lexical._merge(
            lexical._search_documents(
                conn, "s1", "delayed settlement provider", 25, snippet_max_tokens=32
            )
        )
        by_id = {r.id: r for r in results}
        assert by_id["p_relevant"].tier == lexical.RankTier.FTS
        assert by_id["p_noise"].tier == lexical.RankTier.FTS
        assert by_id["p_relevant"].query_tier == lexical.LexicalTier.PHRASE
        assert by_id["p_noise"].query_tier == lexical.LexicalTier.OR_FALLBACK

        ids = [r.id for r in results]
        assert ids.index("p_relevant") < ids.index("p_noise")
    finally:
        conn.close()


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
