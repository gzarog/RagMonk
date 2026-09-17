"""Brute-force cosine similarity, RagMonk's chosen "equivalent embedded
index" (blueprint section 53) in place of a loadable SQLite extension
like sqlite-vec.

Why brute-force over sqlite-vec: ``sqlite3.Connection.enable_load_extension``
is not guaranteed available or enabled on every platform/Python build
(some distro/Homebrew/Windows builds ship SQLite without extension-
loading compiled in at all), and this project has already been burned by
exactly this class of cross-platform SQLite risk in earlier phases (see
CHANGELOG's Phase 5-7 Windows/macOS-specific fixes) -- without a live
multi-OS test to confirm it, wiring in a loadable extension is a real,
unverifiable portability gamble. A plain linear scan has no such risk,
needs no native binary, and is entirely adequate at the scale a local
per-project knowledge base actually holds (thousands, not millions, of
entities/document sections) -- exactly the tradeoff the blueprint itself
sanctions ("sqlite-vec *or equivalent*").

Deliberately pure Python (no numpy): this module is the one piece of
Phase 9 that must stay importable and fast in the *default* test suite
without the real embedding model or its ``torch``/``transformers``
dependency -- see the module docstring split in ``retrieval/embedder.py``.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; ``0.0`` for a length mismatch, an
    empty vector, or a zero vector (undefined direction) rather than
    raising -- a defensively cheap check for what is otherwise a hot
    inner loop.
    """
    if not a or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


@dataclass(frozen=True)
class ScoredCandidate:
    key: str
    score: float


def top_k(
    query_vector: Sequence[float],
    candidates: Sequence[tuple[str, Sequence[float]]],
    *,
    k: int,
    min_score: float = 0.0,
) -> list[ScoredCandidate]:
    """The ``k`` best-scoring candidates at or above ``min_score``,
    ranked by score descending then by key for a deterministic ordering
    among ties (two equally-similar vectors have no other principled
    order, but a stable one keeps repeated runs and ``--json`` output
    reproducible).

    Uses ``heapq.nsmallest`` over ``(-score, key)`` -- exactly the sort key
    that would produce this same ordering via a full sort -- rather than
    sorting every candidate: O(N log K) instead of O(N log N), which
    matters once ``N`` (every current-model embedding) is large and ``K``
    (the caller's requested top-K) stays small (blueprint section 17).
    """
    if k <= 0:
        return []
    scored = (
        ScoredCandidate(key=key, score=score)
        for key, vector in candidates
        if (score := cosine_similarity(query_vector, vector)) >= min_score
    )
    return heapq.nsmallest(k, scored, key=lambda c: (-c.score, c.key))
