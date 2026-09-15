"""Hybrid reranking (blueprint section 21): turns ``retrieval/merger.py``'s
deduplicated candidates into one final ordered list, composed exactly as
the blueprint specifies -- ``primary_tier + secondary_score +
tie_breakers`` -- while preserving deterministic lexical precedence
(section 57's "no half-finished implementation" of "never let semantic
similarity silently upgrade an exact lexical tier").

A candidate found only via semantic search sorts into its own tier
*below every lexical tier* (``_SEMANTIC_ONLY_TIER``), ranked among
itself by score; a candidate found via both keeps its lexical tier as
the primary signal, with its semantic score folded in only as a
same-tier tie-breaker. Ties beyond that fall back to the exact
tie-breaker chain ``lexical.py``'s own ``_sort_key`` already
established (query-structure tier, FTS rank, entity kind, recency,
source, path, id), so identically lexically-tiered results keep the
ordering users already see today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ragpilot.retrieval.lexical import RankTier
from ragpilot.retrieval.merger import SearchCandidate

# One tier below every ``RankTier`` value (``RankTier.PATH`` is the last,
# at 5) -- semantic-only candidates must never outrank even the weakest
# lexical signal, only fill in results lexical search found nothing for.
_SEMANTIC_ONLY_TIER = max(RankTier) + 1


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
        }


def _sort_key(candidate: SearchCandidate) -> tuple[Any, ...]:
    primary_tier = (
        int(candidate.lexical_tier)
        if candidate.lexical_tier is not None
        else int(_SEMANTIC_ONLY_TIER)
    )
    # Descending score as an ascending sort key: higher score first.
    secondary_score = -(candidate.semantic_score if candidate.semantic_score is not None else -1.0)
    return (
        primary_tier,
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
    ranked = sorted(candidates, key=_sort_key)
    hits = [
        RankedHit(
            candidate=c,
            tier_label=c.lexical_tier.name.lower()
            if c.lexical_tier is not None
            else "semantic_only",
        )
        for c in ranked
    ]
    return hits[:limit]
