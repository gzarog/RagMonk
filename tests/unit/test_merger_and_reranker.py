"""Unit tests for ``retrieval/merger.py`` and ``retrieval/reranker.py``
(blueprint sections 21/22; Search Quality Improvement Plan Phase 8):
dedup-by-``(kind, id)`` across lexical and semantic evidence, rank/score
bookkeeping for Reciprocal Rank Fusion, and a final ranking where a
narrow "pinned" tier (exact/qualified/alias symbol, exact title/heading)
always wins unconditionally while everything else -- FTS/path lexical
hits and every semantic hit -- is fused by rank via ``retrieval/
fusion.py``.

Phase 8 deliberately reverses this suite's own previous contract: before
this phase, semantic similarity could never outrank *any* lexical tier,
not just the pinned ones, and several tests below asserted exactly that
as their whole point. Those tests are updated here, not silently
deleted -- see each one's comment for what changed and why.
"""

from __future__ import annotations

from ragmonk.retrieval import fusion
from ragmonk.retrieval.lexical import RankTier, SearchResult
from ragmonk.retrieval.merger import SearchCandidate, merge
from ragmonk.retrieval.reranker import rerank
from ragmonk.retrieval.semantic import SemanticHit


def _lexical(kind: str, id_: str, tier: RankTier, **kwargs: object) -> SearchResult:
    return SearchResult(
        kind=kind, tier=tier, id=id_, title=id_, path=f"/{id_}", source_id="s1", **kwargs
    )


def _semantic(kind: str, id_: str, score: float) -> SemanticHit:
    return SemanticHit(kind=kind, id=id_, title=id_, path=f"/{id_}", source_id="s1", score=score)


# --- merger.merge ------------------------------------------------------


def test_merge_combines_a_hit_found_by_both_signals() -> None:
    lexical_results = [_lexical("entity", "e1", RankTier.FTS)]
    semantic_hits = [_semantic("entity", "e1", 0.9)]
    candidates = merge(lexical_results, semantic_hits)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.lexical_tier == RankTier.FTS
    assert candidate.semantic_score == 0.9
    assert candidate.lexical_rank == 1
    assert candidate.semantic_rank == 1
    # RankTier.FTS is below the pinned tier -- found by both signals or
    # not, it is still a hybrid-tier candidate, not an exact match.
    assert candidate.exact_match is False


def test_merge_keeps_semantic_only_hits_separate() -> None:
    lexical_results = [_lexical("entity", "e1", RankTier.EXACT_SYMBOL)]
    semantic_hits = [_semantic("entity", "e2", 0.5)]
    candidates = merge(lexical_results, semantic_hits)
    by_id = {c.id: c for c in candidates}
    assert by_id["e1"].semantic_score is None
    assert by_id["e1"].semantic_rank is None
    assert by_id["e1"].exact_match is True
    assert by_id["e2"].lexical_tier is None
    assert by_id["e2"].lexical_rank is None
    assert by_id["e2"].semantic_rank == 1
    assert by_id["e2"].exact_match is False


def test_merge_assigns_lexical_rank_from_input_order() -> None:
    lexical_results = [
        _lexical("entity", "first", RankTier.FTS),
        _lexical("entity", "second", RankTier.FTS),
        _lexical("entity", "third", RankTier.FTS),
    ]
    candidates = {c.id: c for c in merge(lexical_results, [])}
    assert candidates["first"].lexical_rank == 1
    assert candidates["second"].lexical_rank == 2
    assert candidates["third"].lexical_rank == 3


def test_merge_assigns_semantic_rank_by_score_regardless_of_input_order() -> None:
    """``semantic_search`` always returns hits sorted by score descending,
    but ``merge`` re-sorts defensively rather than trusting caller order
    -- ranks must reflect actual score order even if a caller (like this
    test) hands them in some other order.
    """
    semantic_hits = [
        _semantic("entity", "low", 0.1),
        _semantic("entity", "high", 0.9),
        _semantic("entity", "mid", 0.5),
    ]
    candidates = {c.id: c for c in merge([], semantic_hits)}
    assert candidates["high"].semantic_rank == 1
    assert candidates["mid"].semantic_rank == 2
    assert candidates["low"].semantic_rank == 3


def test_merge_marks_exact_match_only_for_the_pinned_lexical_tiers() -> None:
    pinned_tiers = [
        RankTier.EXACT_SYMBOL,
        RankTier.QUALIFIED_SYMBOL,
        RankTier.ALIAS_SYMBOL,
        RankTier.TITLE_OR_HEADING,
    ]
    hybrid_tiers = [RankTier.FTS, RankTier.PATH]

    for tier in pinned_tiers:
        candidate = merge([_lexical("entity", "x", tier)], [])[0]
        assert candidate.exact_match is True, tier

    for tier in hybrid_tiers:
        candidate = merge([_lexical("entity", "x", tier)], [])[0]
        assert candidate.exact_match is False, tier


def test_merge_respects_candidate_budgets() -> None:
    lexical_results = [
        _lexical("entity", f"lex{i}", RankTier.FTS)
        for i in range(fusion.MAX_LEXICAL_CANDIDATES + 5)
    ]
    semantic_hits = [
        _semantic("entity", f"sem{i}", score=1.0 - i * 0.001)
        for i in range(fusion.MAX_SEMANTIC_CANDIDATES + 5)
    ]
    candidates = merge(lexical_results, semantic_hits)
    by_id = {c.id: c for c in candidates}

    # The tail beyond each signal's own budget never enters the
    # candidate pool at all -- not merely unranked, genuinely absent.
    assert "lex" + str(fusion.MAX_LEXICAL_CANDIDATES + 4) not in by_id
    assert "sem" + str(fusion.MAX_SEMANTIC_CANDIDATES + 4) not in by_id
    assert f"lex{fusion.MAX_LEXICAL_CANDIDATES - 1}" in by_id
    assert f"sem{fusion.MAX_SEMANTIC_CANDIDATES - 1}" in by_id
    assert len(candidates) == fusion.MAX_LEXICAL_CANDIDATES + fusion.MAX_SEMANTIC_CANDIDATES


# --- fusion.rrf_score ----------------------------------------------------


def test_lexical_only_candidate_gets_a_valid_rrf_score_with_semantic_term_omitted() -> None:
    lexical_results = [_lexical("entity", "e_lex", RankTier.FTS)]
    candidate = merge(lexical_results, [])[0]
    ranked = rerank([candidate], limit=10)
    assert ranked[0].candidate.rrf_score == 1.0 / (fusion.RRF_K + 1)


def test_semantic_only_candidate_gets_a_valid_rrf_score_with_lexical_term_omitted() -> None:
    semantic_hits = [_semantic("entity", "e_sem", 0.42)]
    candidate = merge([], semantic_hits)[0]
    ranked = rerank([candidate], limit=10)
    assert ranked[0].candidate.rrf_score == 1.0 / (fusion.RRF_K + 1)


def test_rrf_score_combines_both_ranks_when_found_by_both_signals() -> None:
    """Hand-computed example: a hit at lexical rank 3 and semantic rank 2
    scores ``1/(60+3) + 1/(60+2)`` -- both terms present, neither
    overwriting the other.
    """
    lexical_results = [
        _lexical("entity", "other1", RankTier.FTS),
        _lexical("entity", "other2", RankTier.FTS),
        _lexical("entity", "target", RankTier.FTS),
    ]
    semantic_hits = [
        _semantic("entity", "other3", 0.99),
        _semantic("entity", "target", 0.9),
    ]
    candidates = merge(lexical_results, semantic_hits)
    target = next(c for c in candidates if c.id == "target")
    assert target.lexical_rank == 3
    assert target.semantic_rank == 2

    ranked = {h.candidate.id: h for h in rerank(candidates, limit=10)}
    expected = 1.0 / (fusion.RRF_K + 3) + 1.0 / (fusion.RRF_K + 2)
    assert ranked["target"].candidate.rrf_score == expected


# --- reranker.rerank -----------------------------------------------------


def test_pinned_tier_always_outranks_hybrid_tier_regardless_of_semantic_score() -> None:
    """The mechanism this phase preserves: an exact/qualified/alias
    symbol or exact title/heading match is never fused, no matter how
    strong a competing semantic-only score is.
    """
    lexical_results = [_lexical("entity", "e_exact", RankTier.EXACT_SYMBOL)]
    semantic_hits = [_semantic("entity", "e_strong_semantic", 0.999)]
    candidates = merge(lexical_results, semantic_hits)
    ranked = rerank(candidates, limit=10)
    assert [h.candidate.id for h in ranked] == ["e_exact", "e_strong_semantic"]
    assert ranked[0].tier_label == "exact_symbol"
    assert ranked[0].candidate.rrf_score is None
    assert ranked[1].tier_label == "hybrid"


def test_hybrid_tier_lets_a_strong_semantic_match_outrank_a_weak_lexical_match() -> None:
    """Phase 8's actual behavior change, replacing this suite's old
    ``test_rerank_never_lets_semantic_outrank_a_lexical_tier`` (which
    asserted the exact opposite -- a lexical ``RankTier.PATH`` hit always
    beat any semantic-only hit). ``RankTier.PATH`` is not a pinned tier,
    so it is now fused: a weak lexical match buried near the bottom of a
    50-deep lexical candidate list loses to a semantic-only match found
    at rank 1, by real RRF arithmetic (not a coincidental tie-break).
    """
    padding = [_lexical("entity", f"pad{i}", RankTier.PATH) for i in range(49)]
    lexical_results = [*padding, _lexical("entity", "e_weak", RankTier.PATH)]
    semantic_hits = [_semantic("entity", "e_strong_semantic", 0.9)]

    candidates = merge(lexical_results, semantic_hits)
    weak = next(c for c in candidates if c.id == "e_weak")
    assert weak.lexical_rank == 50

    ranked = rerank(candidates, limit=5)
    assert ranked[0].candidate.id == "e_strong_semantic"
    assert ranked[0].tier_label == "hybrid"

    strong_rrf = 1.0 / (fusion.RRF_K + 1)
    weak_rrf = 1.0 / (fusion.RRF_K + 50)
    assert strong_rrf > weak_rrf
    assert ranked[0].candidate.rrf_score == strong_rrf


def test_rerank_orders_semantic_only_hits_by_score_descending() -> None:
    semantic_hits = [
        _semantic("entity", "low", 0.1),
        _semantic("entity", "high", 0.9),
        _semantic("entity", "mid", 0.5),
    ]
    candidates = merge([], semantic_hits)
    ranked = rerank(candidates, limit=10)
    assert [h.candidate.id for h in ranked] == ["high", "mid", "low"]


def test_rerank_preserves_lexical_tier_ordering_among_lexical_hits() -> None:
    lexical_results = [
        _lexical("entity", "e_fts", RankTier.FTS),
        _lexical("entity", "e_exact", RankTier.EXACT_SYMBOL),
    ]
    candidates = merge(lexical_results, [])
    ranked = rerank(candidates, limit=10)
    assert [h.candidate.id for h in ranked] == ["e_exact", "e_fts"]


def test_rerank_respects_limit() -> None:
    semantic_hits = [_semantic("entity", f"e{i}", float(i)) for i in range(5)]
    candidates = merge([], semantic_hits)
    ranked = rerank(candidates, limit=2)
    assert len(ranked) == 2
    assert ranked[0].candidate.id == "e4"


def test_rerank_caps_the_hybrid_pool_at_max_fusion_candidates() -> None:
    """Feeds ``reranker.rerank`` already-merged hybrid-tier candidates
    directly (bypassing ``merger.merge``'s own per-signal budgets), so
    the cap under test is ``reranker``'s explicit
    ``fusion.MAX_FUSION_CANDIDATES`` slice, not merge's.
    """
    oversized_pool = fusion.MAX_FUSION_CANDIDATES + 10
    candidates = [
        SearchCandidate(
            kind="entity",
            id=f"c{i}",
            title=f"c{i}",
            path=f"/c{i}",
            source_id="s1",
            lexical_tier=RankTier.PATH,
            lexical_rank=i + 1,
        )
        for i in range(oversized_pool)
    ]
    ranked = rerank(candidates, limit=oversized_pool)
    assert len(ranked) == fusion.MAX_FUSION_CANDIDATES
    # The best-ranked (lowest lexical_rank -> highest RRF score)
    # candidates survive the cap, not an arbitrary prefix/suffix.
    ranked_ids = {h.candidate.id for h in ranked}
    assert "c0" in ranked_ids
    assert f"c{oversized_pool - 1}" not in ranked_ids


def test_ranked_hit_to_dict_shape() -> None:
    candidates = merge([_lexical("entity", "e1", RankTier.EXACT_SYMBOL)], [])
    ranked = rerank(candidates, limit=10)
    payload = ranked[0].to_dict()
    assert payload["kind"] == "entity"
    assert payload["id"] == "e1"
    assert payload["tier"] == "exact_symbol"
    assert payload["semantic_score"] is None
    # Pinned candidates are never fused -- their rrf_score stays unset.
    assert payload["rrf_score"] is None
