"""Proves the query planner's rule-based classification matches the
blueprint's own illustrative examples (section 25), plus a few
supporting cases for each rule.
"""

from __future__ import annotations

from ragmonk.retrieval.planner import Intent, Strategy, plan


def test_bare_identifier_is_identifier_plus_fts() -> None:
    result = plan("BetSettled")
    assert result.intent == Intent.IDENTIFIER
    assert result.strategies == (Strategy.IDENTIFIER, Strategy.FTS)
    assert result.symbol == "BetSettled"


def test_who_calls_is_symbol_plus_incoming_calls() -> None:
    result = plan("who calls SettlementService?")
    assert result.intent == Intent.CALLERS
    assert result.strategies == (Strategy.IDENTIFIER, Strategy.CALLERS)
    assert result.symbol == "SettlementService"


def test_documents_about_is_fts_only_when_semantic_disabled() -> None:
    result = plan("documents about delayed settlement")
    assert result.intent == Intent.DOCUMENT
    assert result.strategies == (Strategy.FTS, Strategy.DOCUMENTS)


def test_documents_about_adds_semantic_when_enabled() -> None:
    result = plan("documents about delayed settlement", semantic_enabled=True)
    assert result.intent == Intent.DOCUMENT
    assert result.strategies == (Strategy.FTS, Strategy.DOCUMENTS, Strategy.SEMANTIC)


def test_what_breaks_if_is_full_impact_strategy_set() -> None:
    result = plan("what breaks if BetSettled changes?")
    assert result.intent == Intent.IMPACT
    assert result.strategies == (
        Strategy.IDENTIFIER,
        Strategy.CALLERS,
        Strategy.CALLEES,
        Strategy.TESTS,
        Strategy.DOCUMENTS,
    )
    assert result.symbol == "BetSettled"


def test_callees_pattern() -> None:
    result = plan("what does SettlementService call")
    assert result.intent == Intent.CALLEES
    assert result.strategies == (Strategy.IDENTIFIER, Strategy.CALLEES)
    assert result.symbol == "SettlementService"


def test_impact_pattern_wins_over_callers_pattern() -> None:
    result = plan("what depends on BetSettled")
    assert result.intent == Intent.IMPACT
    assert result.symbol == "BetSettled"


def test_general_fallback_for_unstructured_phrases() -> None:
    result = plan("how does settlement work overall")
    assert result.intent == Intent.GENERAL
    assert result.strategies == (Strategy.IDENTIFIER, Strategy.FTS, Strategy.REFERENCES)


def test_general_fallback_adds_semantic_when_enabled() -> None:
    result = plan("how does settlement work overall", semantic_enabled=True)
    assert Strategy.SEMANTIC in result.strategies


def test_qualified_name_is_still_identifier_like() -> None:
    result = plan("services.settlement_service.SettlementService")
    assert result.intent == Intent.IDENTIFIER
    assert result.symbol == "services.settlement_service.SettlementService"
