"""Hybrid reranking (blueprint section 21): turns ``retrieval/merger.py``'s
deduplicated candidates into one final ordered list.

Search Quality Improvement Plan, Phase 8 deliberately reverses this
module's own previous design. Until this phase, the module docstring here
read: a candidate found via semantic search sorts into its own tier
*below every lexical tier* (a dedicated ``_SEMANTIC_ONLY_TIER``); a
candidate found via both keeps its lexical tier as the sole primary
signal, semantic score folded in only as a same-tier tie-breaker --
never letting semantic similarity silently upgrade an exact lexical
tier. The user has directed implementing Phase 8 as written in the
search-quality plan despite that conflict: this module now does real
Reciprocal Rank Fusion (``retrieval/fusion.py``) for everything outside
one narrow, still-unconditional exception.

**Pinned tier** (unchanged from before this phase, and the mechanism the
plan itself specifies for keeping exact-match behavior intact): a
candidate whose lexical signal is exact/structural -- ``EXACT_SYMBOL``,
``QUALIFIED_SYMBOL``, ``ALIAS_SYMBOL``, or ``TITLE_OR_HEADING`` (an exact
document title or heading match) -- is flagged ``SearchCandidate.
exact_match`` by ``merger.merge`` and always sorts above every other
candidate, regardless of any semantic score. It is never fused, and
semantic evidence on a pinned candidate still only ever breaks ties
against other pinned candidates in that same tier, exactly as before.

**Hybrid tier** (the actual behavior change): everything else --
``RankTier.FTS``, ``RankTier.PATH``, and every semantic-only hit -- is
now ordered by ``fusion.rrf_score(lexical_rank, semantic_rank)`` instead
of by lexical tier first. A strong semantic-only match can now outrank a
weak lexical-only match here, which was structurally impossible before
this phase. A candidate found by only one signal still gets a valid RRF
score from that signal's term alone (see ``fusion.rrf_score``).

Ties within either tier fall back to the same deterministic tie-breaker
chain ``lexical.py``'s own ``_sort_key`` established (query-structure
tier, FTS rank, entity kind, recency, source, path, id).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ragmonk.retrieval import fusion
from ragmonk.retrieval.merger import SearchCandidate


@dataclass(frozen=True)
class RankedHit:
    candidate: SearchCandidate
    tier_label: str

    def to_dict(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "kind": c.kind,
            "id": c.id,
            "title": c.title,
            "path": c.path,
            "source_id": c.source_id,
            "snippet": c.snippet,
            "location": c.location,
            "tier": self.tier_label,
            "semantic_score": round(c.semantic_score, 4) if c.semantic_score is not None else None,
            "rrf_score": round(c.rrf_score, 6) if c.rrf_score is not None else None,
        }


def _tier_label(candidate: SearchCandidate) -> str:
    if candidate.exact_match:
        assert candidate.lexical_tier is not None
        return candidate.lexical_tier.name.lower()
    return "hybrid"


def _pinned_sort_key(candidate: SearchCandidate) -> tuple[Any, ...]:
    """Same tie-break chain this module always used for lexical tiers
    (``lexical.py``'s own ``_sort_key``), unchanged by Phase 8 -- pinned
    candidates are never fused, so their ordering must stay exactly as
    it was.
    """
    secondary_score = -(candidate.semantic_score if candidate.semantic_score is not None else -1.0)
    return (
        int(candidate.lexical_tier) if candidate.lexical_tier is not None else 0,
        candidate.lexical_query_tier,
        candidate.lexical_fts_rank,
        candidate.entity_kind_rank,
        secondary_score,
        -candidate.mtime,
        candidate.source_id,
        candidate.path,
        candidate.id,
    )


def _hybrid_sort_key(candidate: SearchCandidate) -> tuple[Any, ...]:
    """RRF score first (descending -- a higher fused score wins), then
    the same lower-priority tie-breakers pinned candidates use, for a
    genuine tie between two candidates whose fused scores land exactly
    equal (e.g. both found only at rank 1 of their respective signals).
    """
    secondary_score = -(candidate.semantic_score if candidate.semantic_score is not None else -1.0)
    return (
        -(candidate.rrf_score if candidate.rrf_score is not None else 0.0),
        candidate.lexical_query_tier,
        candidate.lexical_fts_rank,
        candidate.entity_kind_rank,
        secondary_score,
        -candidate.mtime,
        candidate.source_id,
        candidate.path,
        candidate.id,
    )


def rerank(candidates: list[SearchCandidate], *, limit: int) -> list[RankedHit]:
    """Pinned candidates first (unconditionally, tie-broken among
    themselves only), then the hybrid tier ordered by RRF -- see the
    module docstring for what "pinned" means and why.

    The hybrid tier's own candidate pool is capped at ``fusion.
    MAX_FUSION_CANDIDATES`` (after sorting, so it is the *best* 100 that
    survive, not an arbitrary prefix) before ``limit`` is applied to the
    combined list -- in practice ``merger.merge``'s own per-signal
    budgets already keep the hybrid pool at or below that, this is a
    second, explicit guarantee rather than a load-bearing one.
    """
    pinned = sorted((c for c in candidates if c.exact_match), key=_pinned_sort_key)

    hybrid = [
        replace(c, rrf_score=fusion.rrf_score(c.lexical_rank, c.semantic_rank))
        for c in candidates
        if not c.exact_match
    ]
    hybrid.sort(key=_hybrid_sort_key)
    hybrid = hybrid[: fusion.MAX_FUSION_CANDIDATES]

    hits = [RankedHit(candidate=c, tier_label=_tier_label(c)) for c in (*pinned, *hybrid)]
    return hits[:limit]
