"""Retrieval for the admin UI's Search / Explore screens.

Admin UI plan, Phase 5 (§8): a browser way to test retrieval and inspect
why a result came back. Reuses the exact lexical and semantic retrieval
paths ``ragmonk search`` uses -- the UI never re-ranks or re-scores on
its own.
"""

from __future__ import annotations

from typing import Any

from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import lexical, semantic


def _result_row(rank: int, result: lexical.SearchResult) -> dict[str, Any]:
    return {
        "rank": rank,
        "method": "lexical",
        "kind": result.kind,
        "score": None,
        "tier": int(result.tier),
        "id": result.id,
        "title": result.title,
        "path": result.path,
        "source_id": result.source_id,
        "snippet": result.snippet,
    }


def _semantic_row(rank: int, hit: semantic.SemanticHit) -> dict[str, Any]:
    return {
        "rank": rank,
        "method": "semantic",
        "kind": hit.kind,
        "score": round(hit.score, 4),
        "tier": None,
        "id": hit.id,
        "title": hit.title,
        "path": hit.path,
        "source_id": hit.source_id,
        "snippet": hit.snippet,
    }


def run_search(
    ctx: AppContext,
    query: str,
    *,
    mode: str = "lexical",
    limit: int = 20,
) -> dict[str, Any]:
    """Run a search in ``lexical`` | ``semantic`` | ``hybrid`` mode.

    ``semantic`` and ``hybrid`` degrade gracefully: if
    ``search.semantic`` is off or the model/embeddings are unavailable,
    the semantic section reports why (``available``/``reason``) rather
    than erroring, so the Search screen always renders.
    """
    query = query.strip()
    if not query:
        return {"query": query, "mode": mode, "lexical": [], "semantic": None, "reason": None}

    lexical_hits: list[dict[str, Any]] = []
    if mode in ("lexical", "hybrid"):
        results = lexical.search(ctx, query, limit=limit)
        lexical_hits = [_result_row(i + 1, r) for i, r in enumerate(results)]

    semantic_hits: list[dict[str, Any]] | None = None
    reason: str | None = None
    if mode in ("semantic", "hybrid"):
        if not ctx.config.search.semantic:
            reason = "semantic search is disabled (search.semantic = false)"
        else:
            sem = semantic.semantic_search(
                ctx, query, config=ctx.config.search, limit=limit
            )
            if not sem.available:
                reason = sem.reason
                semantic_hits = []
            else:
                semantic_hits = [_semantic_row(i + 1, h) for i, h in enumerate(sem.results)]

    return {
        "query": query,
        "mode": mode,
        "lexical": lexical_hits,
        "semantic": semantic_hits,
        "reason": reason,
    }
