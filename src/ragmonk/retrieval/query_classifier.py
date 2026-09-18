"""Deterministic (no AI) query classification for ``ragmonk search``
(blueprint sections 4/6): what *shape* of query this is, and how much
confidence the lexical pass already found -- both purely a function of
the query text / lexical results already in hand, never a network call
or a model load, so calling this on every search is free.

Two independent, small pieces of information, each with exactly one
consumer:

* ``classify_query`` -- ``QueryKind`` is currently diagnostic-only (see
  ``ragmonk search --explain``), a first step toward the blueprint's
  full per-kind strategy routing (section 4) without yet rewiring which
  strategies ``retrieval/lexical.py`` runs for which kind.
* ``estimate_confidence`` -- ``SearchConfidence`` is what ``cli/search.py``
  uses to decide whether semantic search is worth running at all, when
  ``search.lazy_semantic`` is on (blueprint sections 18/19).
"""

from __future__ import annotations

import re
from enum import StrEnum

from ragmonk.retrieval.lexical import RankTier, SearchResult

_PATH_HINT_RE = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,6}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class QueryKind(StrEnum):
    SYMBOL = "symbol"
    PATH = "path"
    KEYWORD = "keyword"
    CONCEPTUAL = "conceptual"


def classify_query(query: str) -> QueryKind:
    """Classifies ``query`` by shape alone (blueprint section 6):

    * contains a path separator or ends in a short file extension -> PATH
    * a dotted identifier, or a single CamelCase/PascalCase word -> SYMBOL
    * one to three short words (including a single all-lowercase word,
      e.g. "cholesterol") -> KEYWORD
    * anything longer/sentence-shaped -> CONCEPTUAL

    A single lowercase identifier-shaped word is deliberately KEYWORD,
    not SYMBOL: the blueprint's own examples split exactly on
    "CamelCase" ("SettlementService" -> SYMBOL) vs. a plain term
    ("cholesterol" -> KEYWORD) -- bare identifier syntax alone doesn't
    distinguish them, capitalization does.
    """
    text = query.strip()
    if not text:
        return QueryKind.KEYWORD
    if _PATH_HINT_RE.search(text):
        return QueryKind.PATH
    if _IDENTIFIER_RE.match(text) and ("." in text or text != text.lower()):
        return QueryKind.SYMBOL
    words = text.split()
    if len(words) <= 3 and all(len(w) <= 20 for w in words):
        return QueryKind.KEYWORD
    return QueryKind.CONCEPTUAL


class SearchConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Tiers that mean "we already found what the query was almost certainly
# asking for" -- an exact/qualified/alias symbol match or an exact
# title/heading hit (blueprint section 19). ``PATH``/``FTS`` hits alone
# are treated as MEDIUM: a lexical signal exists, but not one strong
# enough to be sure semantic search would add nothing.
_HIGH_CONFIDENCE_TIERS = frozenset(
    {
        RankTier.EXACT_SYMBOL,
        RankTier.QUALIFIED_SYMBOL,
        RankTier.ALIAS_SYMBOL,
        RankTier.TITLE_OR_HEADING,
    }
)


def estimate_confidence(results: list[SearchResult]) -> SearchConfidence:
    """How confident the lexical pass already is, from its own ranked
    results -- the signal ``search.lazy_semantic`` uses to decide whether
    running semantic search too is worth its cost (blueprint section 18).
    """
    if not results:
        return SearchConfidence.LOW
    if any(r.tier in _HIGH_CONFIDENCE_TIERS for r in results):
        return SearchConfidence.HIGH
    return SearchConfidence.MEDIUM
