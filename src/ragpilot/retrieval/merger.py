"""Hybrid candidate merge (blueprint section 22): combines
``retrieval/lexical.py``'s ranked results and ``retrieval/semantic.py``'s
similarity hits into one deduplicated candidate set per ``(kind, id)``,
tracking which signal(s) found each one -- the input ``retrieval/
reranker.py`` reranks into a single ordered list.

Deliberately a separate step from ``lexical.py``'s own ``_merge``: that
function only ever sees lexical evidence and stays untouched (its
existing dedup/tiering behavior, and every test pinned to it, keeps
working exactly as before) -- this module is the *additional* hybrid
view ``ragpilot search`` builds on top, not a replacement for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ragpilot.retrieval.lexical import LexicalTier, RankTier, SearchResult
from ragpilot.retrieval.semantic import SemanticHit


@dataclass(slots=True)
class SearchCandidate:
    """One ``(kind, id)``'s combined evidence -- a lexical tier/rank, a
    semantic score, or both. Never upgrades a lexical tier just because
    semantic evidence also exists (blueprint sections 21/57): the two
    scores stay on separate fields, and ``reranker.py`` reads
    ``lexical_tier`` as the sole primary-ranking signal.
    """

    kind: str
    id: str
    title: str
    path: str
    source_id: str
    snippet: str | None = None
    location: dict[str, object] | None = None
    lexical_tier: RankTier | None = None
    lexical_fts_rank: int = 0
    lexical_query_tier: LexicalTier = LexicalTier.PHRASE
    entity_kind_rank: int = 99
    mtime: float = 0.0
    semantic_score: float | None = None


def merge(
    lexical_results: list[SearchResult], semantic_hits: list[SemanticHit]
) -> list[SearchCandidate]:
    """Deduplicates lexical and semantic hits by ``(kind, id)``. A
    candidate found by both keeps its lexical fields (title/snippet/
    location come from whichever signal found it first; lexical wins
    ties since it is the deterministic, always-on signal) and gains the
    semantic hit's score.
    """
    by_key: dict[tuple[str, str], SearchCandidate] = {}
    for result in lexical_results:
        by_key[(result.kind, result.id)] = SearchCandidate(
            kind=result.kind,
            id=result.id,
            title=result.title,
            path=result.path,
            source_id=result.source_id,
            snippet=result.snippet,
            location=result.location,
            lexical_tier=result.tier,
            lexical_fts_rank=result.fts_rank,
            lexical_query_tier=result.query_tier,
            entity_kind_rank=result.entity_kind_rank,
            mtime=result.mtime,
        )
    for hit in semantic_hits:
        key = (hit.kind, hit.id)
        existing = by_key.get(key)
        if existing is not None:
            existing.semantic_score = hit.score
        else:
            by_key[key] = SearchCandidate(
                kind=hit.kind,
                id=hit.id,
                title=hit.title,
                path=hit.path,
                source_id=hit.source_id,
                snippet=hit.snippet,
                location=hit.location,
                semantic_score=hit.score,
            )
    return list(by_key.values())
