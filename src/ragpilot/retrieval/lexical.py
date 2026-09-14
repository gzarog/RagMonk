"""Lexical search: exact identifiers, ``code_fts``/``document_fts``, file
paths, document titles/headings, and qualified-name aliases, merged into
one ranked list for ``ragpilot search``.

Ranking and merging both live in this one module rather than a separate
``reranker.py``: there is exactly one signal-to-tier mapping and one
merge step in this phase, so splitting them would only add an import hop
with nothing left to decouple.

Ranking order, best first (blueprint order): exact symbol > qualified
symbol > title/heading match > FTS rank > path relevance > entity type >
recency > source priority. The first five are distinct ``RankTier``
values; "entity type"/"recency"/"source priority" have no dedicated
signal stored anywhere in Phases 1-4 to rank *across* tiers on, so they
are folded in as deterministic tie-breakers *within* a tier instead (see
``_sort_key``) -- entity kind via a fixed structural-to-narrow ordering,
recency via each result's file mtime, and source priority via the
registered source id (the closest thing to a priority Phase 1's registry
stores), rather than inventing configuration that doesn't exist yet.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from ragpilot.code.graph import all_project_connections
from ragpilot.core.lifecycle import AppContext
from ragpilot.core.models import EntityType
from ragpilot.retrieval import cache as search_cache
from ragpilot.storage.repositories import documents_repo, entities_repo, files_repo

DEFAULT_LIMIT = 20

_ENTITY_KIND_RANK: dict[EntityType, int] = {
    kind: index
    for index, kind in enumerate(
        [
            EntityType.NAMESPACE,
            EntityType.CLASS,
            EntityType.INTERFACE,
            EntityType.STRUCT,
            EntityType.ENUM,
            EntityType.FUNCTION,
            EntityType.METHOD,
            EntityType.PROPERTY,
            EntityType.FIELD,
        ]
    )
}

class RankTier(IntEnum):
    EXACT_SYMBOL = 0
    QUALIFIED_SYMBOL = 1
    ALIAS_SYMBOL = 2
    TITLE_OR_HEADING = 3
    FTS = 4
    PATH = 5


@dataclass(frozen=True)
class SearchResult:
    kind: str  # "entity" | "document" | "path"
    tier: RankTier
    id: str
    title: str
    path: str
    source_id: str
    snippet: str | None = None
    location: dict[str, Any] | None = None
    fts_rank: int = 0
    entity_kind_rank: int = 99
    mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": self.tier.name.lower(),
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "source_id": self.source_id,
            "snippet": self.snippet,
            "location": self.location,
        }


@dataclass(frozen=True)
class StageTiming:
    """One named stage's wall-clock cost, in milliseconds -- the raw
    material for ``ragpilot search --explain`` (blueprint section 33).
    """

    name: str
    hits: int
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "hits": self.hits, "duration_ms": round(self.duration_ms, 3)}


@dataclass(frozen=True)
class TimedSearchResult:
    """``search()``'s results plus a per-stage timing breakdown -- kept
    as a separate, opt-in return type (see ``search_with_timings``)
    rather than changing ``search()``'s own signature, so every existing
    caller that only wants the ranked list is unaffected.
    """

    results: list[SearchResult]
    timings: list[StageTiming] = field(default_factory=list)


class _StopwatchTimings:
    """Accumulates ``StageTiming`` entries for one ``search()`` call.

    A tiny mutable helper (not a context manager per stage) so the three
    ``_search_*`` functions stay pure "query in, results out" -- one
    ``@_timed`` decorator-style call site per stage in ``search()`` below
    records the elapsed time and hit count without threading a timing
    object through every helper's signature.
    """

    def __init__(self) -> None:
        self.entries: list[StageTiming] = []

    def record(self, name: str, started_at: float, results: list[SearchResult]) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.entries.append(StageTiming(name=name, hits=len(results), duration_ms=elapsed_ms))


def _fts_query(text: str) -> str | None:
    """Builds a permissive FTS5 MATCH expression: each word token quoted
    (so punctuation in the query, e.g. "settlement.BetSettled", can never
    be parsed as FTS syntax) and OR'd together, favoring recall -- the
    ranking below is what narrows a broad FTS hit set back down, not the
    query itself.
    """
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def _contains_ci(haystack: str | None, needle: str) -> bool:
    return haystack is not None and needle.lower() in haystack.lower()


def _sort_key(result: SearchResult) -> tuple[Any, ...]:
    return (
        result.tier,
        result.fts_rank,
        result.entity_kind_rank,
        -result.mtime,
        result.source_id,
        result.path,
        result.id,
    )


def _search_entities(
    conn: sqlite3.Connection, source_id: str, query: str, limit: int
) -> list[SearchResult]:
    """Exact/qualified-name, alias, and FTS entity hits, each row already
    joined against its file (blueprint sections 7/8/9) -- no per-row
    ``files_repo.get`` round trip, and the alias lookup is an indexed
    ``WHERE alias = ?`` seek instead of a full-corpus Python scan.
    """
    results: list[SearchResult] = []
    exact_ids: set[str] = set()
    for row in entities_repo.search_exact_projection(conn, query):
        exact_ids.add(row.id)
        tier = RankTier.EXACT_SYMBOL if row.name == query else RankTier.QUALIFIED_SYMBOL
        results.append(
            SearchResult(
                kind="entity",
                tier=tier,
                id=row.id,
                title=row.qualified_name,
                path=row.path,
                source_id=source_id,
                snippet=row.signature,
                location={"line_start": row.start_line, "line_end": row.end_line},
                entity_kind_rank=_ENTITY_KIND_RANK.get(row.kind, 99),
                mtime=row.mtime,
            )
        )

    # Aliases are always the dotted "Class.member" shape (see
    # ``entities_repo.compute_alias``), so this lookup is only worth
    # issuing when the query itself contains a dot -- bounding it to
    # queries that could plausibly benefit, even though the underlying
    # index makes a miss cheap either way.
    if "." in query:
        for row in entities_repo.search_alias_projection(conn, query, limit=limit):
            if row.id in exact_ids:
                continue
            results.append(
                SearchResult(
                    kind="entity",
                    tier=RankTier.ALIAS_SYMBOL,
                    id=row.id,
                    title=row.qualified_name,
                    path=row.path,
                    source_id=source_id,
                    snippet=row.signature,
                    location={"line_start": row.start_line, "line_end": row.end_line},
                    entity_kind_rank=_ENTITY_KIND_RANK.get(row.kind, 99),
                    mtime=row.mtime,
                )
            )

    fts_query = _fts_query(query)
    if fts_query is not None:
        for row in entities_repo.search_fts_projection(conn, fts_query, limit=limit):
            results.append(
                SearchResult(
                    kind="entity",
                    tier=RankTier.FTS,
                    id=row.id,
                    title=row.qualified_name,
                    path=row.path,
                    source_id=source_id,
                    snippet=row.signature,
                    location={"line_start": row.start_line, "line_end": row.end_line},
                    fts_rank=row.fts_rank,
                    entity_kind_rank=_ENTITY_KIND_RANK.get(row.kind, 99),
                    mtime=row.mtime,
                )
            )
    return results


def _search_documents(
    conn: sqlite3.Connection, source_id: str, query: str, limit: int, *, snippet_max_tokens: int
) -> list[SearchResult]:
    """Exact-title and FTS document hits, each row already joined against
    its file -- replaces the previous ``list_all()`` full-corpus title
    scan and the per-FTS-row ``get_document``/``files_repo.get`` pair.
    """
    results: list[SearchResult] = []
    for row in documents_repo.search_title_projection(conn, query, limit=limit):
        results.append(
            SearchResult(
                kind="document",
                tier=RankTier.TITLE_OR_HEADING,
                id=row.id,
                title=row.title,
                path=row.path,
                source_id=source_id,
                snippet=row.title,
                mtime=row.mtime,
            )
        )

    fts_query = _fts_query(query)
    if fts_query is None:
        return results
    for row in documents_repo.search_fts_projection(
        conn, fts_query, limit=limit, snippet_max_tokens=snippet_max_tokens
    ):
        tier = RankTier.TITLE_OR_HEADING if _contains_ci(row.heading, query) else RankTier.FTS
        results.append(
            SearchResult(
                kind="document",
                tier=tier,
                id=row.id,
                title=row.title,
                path=row.path,
                source_id=source_id,
                snippet=row.snippet,
                location={
                    "section": row.heading,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "heading_path": row.heading_path,
                },
                fts_rank=row.fts_rank,
                mtime=row.mtime,
            )
        )
    return results


def _search_paths(
    conn: sqlite3.Connection, source_id: str, query: str, limit: int
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in files_repo.search_path_projection(conn, query, limit=limit):
        results.append(
            SearchResult(
                kind="path",
                tier=RankTier.PATH,
                id=row.id,
                title=row.path,
                path=row.path,
                source_id=source_id,
                mtime=row.mtime,
            )
        )
    return results


def _merge(results: list[SearchResult]) -> list[SearchResult]:
    """Same (kind, id) found through more than one signal (e.g. an exact
    name match that also showed up in FTS) keeps only its best-tiered
    hit -- this is both the dedup and the final ranking step.
    """
    best: dict[tuple[str, str], SearchResult] = {}
    for result in results:
        key = (result.kind, result.id)
        current = best.get(key)
        if current is None or _sort_key(result) < _sort_key(current):
            best[key] = result
    return sorted(best.values(), key=_sort_key)


def search(ctx: AppContext, query: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchResult]:
    return search_with_timings(ctx, query, limit=limit).results


def search_with_timings(
    ctx: AppContext, query: str, *, limit: int = DEFAULT_LIMIT
) -> TimedSearchResult:
    """Same ranked results as ``search()``, plus a per-stage timing
    breakdown (blueprint sections 33/34) -- ``ragpilot search --explain``
    is the one caller that reads ``.timings``; every other caller keeps
    using the plain ``search()`` wrapper above.

    Consults ``search.cache`` first when enabled (blueprint section 23;
    see ``retrieval/cache.py`` for why this only pays off in a long-lived
    process). A cache hit reports its own lookup cost as a single
    ``cache_hit`` stage rather than replaying the original call's
    per-stage timings, which would misrepresent this call's actual cost.
    """
    query = query.strip()
    if not query:
        return TimedSearchResult(results=[])

    connections = list(all_project_connections(ctx))
    cache_config = ctx.config.search.cache
    snippet_max_tokens = ctx.config.search.output.snippet_max_tokens
    cache_key: str | None = None
    if cache_config.enabled:
        started = time.perf_counter()
        result_cache = search_cache.get_search_result_cache(cache_config.max_queries)
        cache_key = search_cache.search_cache_key(
            query=query, mode="lexical", limit=limit, connections=[c for _, _, c in connections]
        )
        cached = result_cache.get(cache_key)
        if cached is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return TimedSearchResult(
                results=cached,
                timings=[StageTiming(name="cache_hit", hits=len(cached), duration_ms=elapsed_ms)],
            )

    stopwatch = _StopwatchTimings()
    collected: list[SearchResult] = []
    for source_id, _source_path, conn in connections:
        started = time.perf_counter()
        entity_hits = _search_entities(conn, source_id, query, limit)
        stopwatch.record("entities", started, entity_hits)
        collected.extend(entity_hits)

        started = time.perf_counter()
        document_hits = _search_documents(
            conn, source_id, query, limit, snippet_max_tokens=snippet_max_tokens
        )
        stopwatch.record("documents", started, document_hits)
        collected.extend(document_hits)

        started = time.perf_counter()
        path_hits = _search_paths(conn, source_id, query, limit)
        stopwatch.record("paths", started, path_hits)
        collected.extend(path_hits)

    started = time.perf_counter()
    merged = _merge(collected)[:limit]
    stopwatch.record("merge", started, merged)

    if cache_config.enabled and cache_key is not None:
        search_cache.get_search_result_cache(cache_config.max_queries).set(cache_key, merged)

    return TimedSearchResult(results=merged, timings=stopwatch.entries)
