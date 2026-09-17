"""Centralizes RagMonk's confidence ladder.

``core.models.Confidence`` is the single enum (defined there since Phase
2, not duplicated here) -- this module only adds the ordering and
comparison helpers so Phase 2's resolver, Phase 4's linker, and any later
phase (Phase 5's context builder, Phase 6's MCP tools) reference one
shared "how do these tiers compare" definition instead of each hand-
rolling their own.

Ordering, highest trust first: EXACT > HIGH > MEDIUM > HEURISTIC. This
mirrors ``code/resolver.py``'s own comment (EXACT/HIGH/MEDIUM roughly
"how sure are we", HEURISTIC set aside for framework-pattern guesses
regardless of how confident the guess feels) -- HEURISTIC sits below
MEDIUM here because a naming-convention guess is weaker evidence than
even an ambiguous-but-still-a-real-identifier-match resolution.
"""

from __future__ import annotations

from collections.abc import Iterable

from ragmonk.core.models import Confidence

_RANK: dict[Confidence, int] = {
    Confidence.HEURISTIC: 0,
    Confidence.MEDIUM: 1,
    Confidence.HIGH: 2,
    Confidence.EXACT: 3,
}


def rank(confidence: Confidence) -> int:
    """An integer ordinal for ``confidence``, higher meaning more trusted."""
    return _RANK[confidence]


def at_least(confidence: Confidence, floor: Confidence) -> bool:
    """True when ``confidence`` is at least as trustworthy as ``floor``."""
    return rank(confidence) >= rank(floor)


def highest(confidences: Iterable[Confidence]) -> Confidence | None:
    """The single most-trusted tier among ``confidences``, or ``None`` for
    an empty iterable.
    """
    best: Confidence | None = None
    for c in confidences:
        if best is None or rank(c) > rank(best):
            best = c
    return best
