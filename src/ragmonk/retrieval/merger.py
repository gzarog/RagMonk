"""Hybrid candidate merge (blueprint section 22): combines
``retrieval/lexical.py``'s ranked results and ``retrieval/semantic.py``'s
similarity hits into one deduplicated candidate set per ``(kind, id)``,
tracking which signal(s) found each one, at what rank, and with what raw
score -- the input ``retrieval/reranker.py`` reranks into a single
ordered list (Phase 8: real Reciprocal Rank Fusion for everything outside
the pinned exact-match tier -- see ``reranker.py``'s module docstring and
``retrieval/fusion.py``).

Deliberately a separate step from ``lexical.py``'s own ``_merge``: that
function only ever sees lexical evidence and stays untouched (its
existing dedup/tiering behavior, and every test pinned to it, keeps
working exactly as before) -- this module is the *additional* hybrid
view ``ragmonk search`` builds on top, not a replacement for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ragmonk.retrieval.fusion import MAX_LEXICAL_CANDIDATES, MAX_SEMANTIC_CANDIDATES
from ragmonk.retrieval.lexical import LexicalTier, RankTier, SearchResult
from ragmonk.retrieval.semantic import SemanticHit

# The pinned tier (Phase 8): a lexical hit this exact/structural
# (strictly better than ``RankTier.FTS``) is never fused -- it wins
# unconditionally, exactly as every tier at or above ``TITLE_OR_HEADING``
# already did before this phase. ``RankTier.FTS``/``RankTier.PATH`` and
# every semantic-only hit fall through to RRF instead. See ``reranker.
# py``'s module docstring for the full rationale.
_PINNED_MAX_TIER = RankTier.FTS


@dataclass(slots=True)
class SearchCandidate:
    """One ``(kind, id)``'s combined evidence -- a lexical tier/rank, a
    semantic score, or both, every signal preserved on its own field
    rather than collapsed into one (Phase 8's candidate shape). Nothing
    here overwrites another field: ``lexical_tier``/``lexical_rank``/
    ``bm25_score`` are the lexical signal, ``semantic_rank``/
    ``semantic_score`` are the semantic one, and ``rrf_score`` (set by
    ``reranker.rerank`` via ``fusion.rrf_score``, ``None`` until then) is
    their fused combination -- ``reranker.py`` reads ``exact_match`` to
    decide whether a candidate bypasses fusion entirely (the pinned
    tier) or is ordered by ``rrf_score`` alongside every other hybrid-
    tier candidate.
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
    # This candidate's 1-based position within the (already lexically
    # ranked) ``lexical_results`` list passed to ``merge`` -- the "rank"
    # half of Phase 8's RRF formula. ``None`` for a semantic-only
    # candidate.
    lexical_rank: int | None = None
    # The raw ``bm25()`` value behind ``lexical_fts_rank``'s ordinal
    # position (see ``lexical.SearchResult.bm25_score``) -- informational
    # only, never itself part of the RRF sum.
    bm25_score: float | None = None
    semantic_score: float | None = None
    # This candidate's 1-based position within ``semantic_hits`` once
    # sorted by score descending -- RRF's other rank term. ``None`` for a
    # lexical-only candidate.
    semantic_rank: int | None = None
    # Never upgraded by fusion -- see ``_PINNED_MAX_TIER`` above and
    # ``reranker.py``'s pinned/hybrid split.
    exact_match: bool = False
    # Populated by ``reranker.rerank`` (via ``fusion.rrf_score``), not by
    # ``merge`` itself -- rank assignment (this module) and the RRF
    # arithmetic that consumes it (``fusion.py``) are deliberately kept
    # separate, see ``fusion.py``'s own module docstring.
    rrf_score: float | None = None


def merge(
    lexical_results: list[SearchResult], semantic_hits: list[SemanticHit]
) -> list[SearchCandidate]:
    """Deduplicates lexical and semantic hits by ``(kind, id)``. A
    candidate found by both keeps its lexical fields (title/snippet/
    location come from whichever signal found it first; lexical wins
    ties since it is the deterministic, always-on signal) and gains the
    semantic hit's score/rank.

    Both inputs are assumed already ranked by their own signal (``
    lexical.search``'s multi-key tie-break chain; ``semantic_search``'s
    score-descending sort) -- ``lexical_rank`` is assigned from
    ``lexical_results``'s given order as-is (re-deriving that tie-break
    chain here would duplicate logic ``lexical.py``'s own module
    docstring already warns against), while ``semantic_hits`` is
    re-sorted by score descending first so a caller's list order can
    never silently invert semantic ranks.

    Each input is capped at its own Phase 8 candidate budget (``fusion.
    MAX_LEXICAL_CANDIDATES``/``MAX_SEMANTIC_CANDIDATES``) before ranks
    are assigned -- RRF needs a genuine rank within a wide-enough pool,
    but an unbounded one would cost real DB/ANN time for no ranking
    benefit past a point; the caller's own ``lexical.search``/
    ``semantic_search`` calls already do the actual DB/ANN work, this
    only bounds how much of what they returned gets folded in here.
    """
    lexical_results = lexical_results[:MAX_LEXICAL_CANDIDATES]
    semantic_hits = sorted(semantic_hits, key=lambda h: (-h.score, h.path, h.id))[
        :MAX_SEMANTIC_CANDIDATES
    ]

    by_key: dict[tuple[str, str], SearchCandidate] = {}
    for lexical_rank, result in enumerate(lexical_results, start=1):
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
            lexical_rank=lexical_rank,
            bm25_score=result.bm25_score,
            exact_match=result.tier < _PINNED_MAX_TIER,
        )
    for semantic_rank, hit in enumerate(semantic_hits, start=1):
        key = (hit.kind, hit.id)
        existing = by_key.get(key)
        if existing is not None:
            existing.semantic_score = hit.score
            existing.semantic_rank = semantic_rank
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
                semantic_rank=semantic_rank,
            )
    return list(by_key.values())
