"""Unit tests for ``retrieval/fusion.py`` (Search Quality Improvement
Plan, Phase 8): the pure Reciprocal Rank Fusion arithmetic and its
candidate-budget constants, independent of ``SearchCandidate``/
``merge``/``rerank`` -- see ``tests/unit/test_merger_and_reranker.py``
for those integration-level behaviors.
"""

from __future__ import annotations

from ragpilot.retrieval import fusion


def test_rrf_k_default_is_the_plans_starting_point() -> None:
    assert fusion.RRF_K == 60


def test_candidate_budget_constants_match_the_plans_starting_point() -> None:
    assert fusion.MAX_LEXICAL_CANDIDATES == 50
    assert fusion.MAX_SEMANTIC_CANDIDATES == 50
    assert fusion.MAX_FUSION_CANDIDATES == 100


def test_rrf_score_with_both_ranks_sums_both_terms() -> None:
    assert fusion.rrf_score(1, 1) == 1.0 / 61 + 1.0 / 61


def test_rrf_score_with_only_lexical_rank_omits_the_semantic_term() -> None:
    assert fusion.rrf_score(5, None) == 1.0 / 65


def test_rrf_score_with_only_semantic_rank_omits_the_lexical_term() -> None:
    assert fusion.rrf_score(None, 3) == 1.0 / 63


def test_rrf_score_with_neither_rank_is_zero() -> None:
    assert fusion.rrf_score(None, None) == 0.0


def test_rrf_score_respects_a_custom_k() -> None:
    assert fusion.rrf_score(1, None, k=10) == 1.0 / 11
    assert fusion.rrf_score(1, None, k=10) != fusion.rrf_score(1, None)


def test_rrf_score_decreases_monotonically_as_rank_worsens() -> None:
    scores = [fusion.rrf_score(rank, None) for rank in (1, 2, 5, 10, 50)]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)


def test_rrf_score_rewards_being_found_by_both_signals_over_either_alone() -> None:
    lexical_only = fusion.rrf_score(1, None)
    semantic_only = fusion.rrf_score(None, 1)
    both = fusion.rrf_score(1, 1)
    assert both > lexical_only
    assert both > semantic_only
