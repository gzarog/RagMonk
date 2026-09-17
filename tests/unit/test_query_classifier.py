"""Unit tests for ``retrieval/query_classifier.py``'s deterministic query
shape classification and lexical-confidence estimation (blueprint
sections 6/19).
"""

from __future__ import annotations

from ragmonk.retrieval import query_classifier
from ragmonk.retrieval.lexical import RankTier, SearchResult


def _result(tier: RankTier) -> SearchResult:
    return SearchResult(kind="entity", tier=tier, id="x", title="x", path="x", source_id="s")


def test_classify_query_detects_path() -> None:
    assert (
        query_classifier.classify_query("retrieval/vectorstore.py")
        == query_classifier.QueryKind.PATH
    )
    assert (
        query_classifier.classify_query("SettlementService.cs") == query_classifier.QueryKind.PATH
    )


def test_classify_query_detects_symbol() -> None:
    assert query_classifier.classify_query("SettlementService") == query_classifier.QueryKind.SYMBOL
    assert (
        query_classifier.classify_query("SettlementService.Process")
        == query_classifier.QueryKind.SYMBOL
    )


def test_classify_query_detects_keyword() -> None:
    assert query_classifier.classify_query("cholesterol") == query_classifier.QueryKind.KEYWORD
    assert query_classifier.classify_query("rabbitmq retry") == query_classifier.QueryKind.KEYWORD


def test_classify_query_detects_conceptual() -> None:
    assert (
        query_classifier.classify_query("which documents discuss cardiovascular risk")
        == query_classifier.QueryKind.CONCEPTUAL
    )
    assert (
        query_classifier.classify_query("how are provider settlements handled")
        == query_classifier.QueryKind.CONCEPTUAL
    )


def test_confidence_is_low_with_no_results() -> None:
    assert query_classifier.estimate_confidence([]) == query_classifier.SearchConfidence.LOW


def test_confidence_is_high_for_exact_symbol() -> None:
    results = [_result(RankTier.EXACT_SYMBOL), _result(RankTier.FTS)]
    assert query_classifier.estimate_confidence(results) == query_classifier.SearchConfidence.HIGH


def test_confidence_is_medium_for_fts_only() -> None:
    results = [_result(RankTier.FTS), _result(RankTier.PATH)]
    assert query_classifier.estimate_confidence(results) == query_classifier.SearchConfidence.MEDIUM
