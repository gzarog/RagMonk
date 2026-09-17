"""Deterministic (no LLM) query planner -- decides which retrieval
strategies ``ragmonk explore`` (and, indirectly, ``impact``) should run
for a given query, per the blueprint's own examples (section 25):

    "BetSettled"                          -> exact identifier lookup + FTS
    "who calls SettlementService?"        -> symbol lookup + incoming CALLS
    "documents about delayed settlement"  -> FTS (+ semantic if enabled)
    "what breaks if BetSettled changes?"  -> symbol + graph + tests + docs

This is deliberately a small, ordered set of regex rules, not an NLP
query-understanding system -- the blueprint asks for "initially
deterministic" classification, and every rule here maps directly to one
of the four examples above (impact-shaped phrasings checked before
callers/callees so "what breaks/depends on" doesn't fall through to the
narrower "who calls" rule, since both mention a symbol being acted on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Strategy(StrEnum):
    IDENTIFIER = "identifier"
    FTS = "fts"
    CALLERS = "graph_callers"
    CALLEES = "graph_callees"
    REFERENCES = "graph_references"
    TESTS = "tests"
    DOCUMENTS = "documents"
    SEMANTIC = "semantic"


class Intent(StrEnum):
    IDENTIFIER = "identifier"
    CALLERS = "callers"
    CALLEES = "callees"
    IMPACT = "impact"
    DOCUMENT = "document"
    GENERAL = "general"


@dataclass(frozen=True)
class QueryPlan:
    query: str
    intent: Intent
    strategies: tuple[Strategy, ...]
    symbol: str | None = None


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# Tried in this order; the first pattern to match a symbol wins. Impact
# phrasings are checked first since "what breaks if X changes" and "who
# calls X" both talk about a symbol but call for very different strategy
# sets -- the more specific (impact) pattern has to win the race.
_IMPACT_PATTERNS = (
    re.compile(r"what breaks if ([\w.]+)", re.IGNORECASE),
    re.compile(r"what happens if ([\w.]+) changes", re.IGNORECASE),
    re.compile(r"impact of ([\w.]+)", re.IGNORECASE),
    re.compile(r"what depends on ([\w.]+)", re.IGNORECASE),
)
_CALLERS_PATTERNS = (
    re.compile(r"who calls ([\w.]+)", re.IGNORECASE),
    re.compile(r"callers of ([\w.]+)", re.IGNORECASE),
    re.compile(r"what calls ([\w.]+)", re.IGNORECASE),
    re.compile(r"who (?:uses|references) ([\w.]+)", re.IGNORECASE),
)
_CALLEES_PATTERNS = (
    re.compile(r"what does ([\w.]+) call", re.IGNORECASE),
    re.compile(r"callees of ([\w.]+)", re.IGNORECASE),
    re.compile(r"what ([\w.]+) calls", re.IGNORECASE),
)
_DOCUMENT_HINT_RE = re.compile(r"\bdocuments?\b|\bdocs?\b|\bdocumentation\b", re.IGNORECASE)


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def plan(query: str, *, semantic_enabled: bool = False) -> QueryPlan:
    """Classifies ``query`` into an intent and an ordered set of
    strategies. ``semantic_enabled`` should be ``ctx.config.search.
    semantic`` -- it only ever *adds* ``Strategy.SEMANTIC`` to a document/
    general plan, never changes which intent a query gets, so a caller
    with semantic search off (the Phase 5 default) sees exactly the
    strategy sets the blueprint's examples describe.
    """
    text = query.strip()

    symbol = _first_match(_IMPACT_PATTERNS, text)
    if symbol:
        return QueryPlan(
            query=query,
            intent=Intent.IMPACT,
            strategies=(
                Strategy.IDENTIFIER,
                Strategy.CALLERS,
                Strategy.CALLEES,
                Strategy.TESTS,
                Strategy.DOCUMENTS,
            ),
            symbol=symbol,
        )

    symbol = _first_match(_CALLERS_PATTERNS, text)
    if symbol:
        return QueryPlan(
            query=query,
            intent=Intent.CALLERS,
            strategies=(Strategy.IDENTIFIER, Strategy.CALLERS),
            symbol=symbol,
        )

    symbol = _first_match(_CALLEES_PATTERNS, text)
    if symbol:
        return QueryPlan(
            query=query,
            intent=Intent.CALLEES,
            strategies=(Strategy.IDENTIFIER, Strategy.CALLEES),
            symbol=symbol,
        )

    if _DOCUMENT_HINT_RE.search(text):
        strategies: tuple[Strategy, ...] = (Strategy.FTS, Strategy.DOCUMENTS)
        if semantic_enabled:
            strategies += (Strategy.SEMANTIC,)
        return QueryPlan(query=query, intent=Intent.DOCUMENT, strategies=strategies, symbol=None)

    if _IDENTIFIER_RE.match(text):
        return QueryPlan(
            query=query,
            intent=Intent.IDENTIFIER,
            strategies=(Strategy.IDENTIFIER, Strategy.FTS),
            symbol=text,
        )

    strategies = (Strategy.IDENTIFIER, Strategy.FTS, Strategy.REFERENCES)
    if semantic_enabled:
        strategies += (Strategy.SEMANTIC,)
    return QueryPlan(query=query, intent=Intent.GENERAL, strategies=strategies, symbol=None)
