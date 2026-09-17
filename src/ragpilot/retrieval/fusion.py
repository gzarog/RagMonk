"""Reciprocal Rank Fusion (Search Quality Improvement Plan, Phase 8):
the pure rank-arithmetic and candidate-budget constants ``retrieval/
merger.py``/``retrieval/reranker.py`` build their hybrid tier on.

This phase deliberately reverses ``reranker.py``'s previous, documented
design (semantic similarity as a same-tier tie-breaker only, never able
to outrank a weaker lexical tier). A candidate found by an *exact*
lexical signal (symbol/qualified-symbol/alias/title/heading -- Phase 8's
"pinned tier", enforced in ``reranker.py`` via ``SearchCandidate.
exact_match``) still always wins, unconditionally; everything else --
FTS/path lexical hits and every semantic hit -- is now fused by rank
instead of kept in strictly separate, one-directional tiers. See
``reranker.py``'s own module docstring for the full before/after and
``CHANGELOG.md``'s Phase 8 entry for why.

Kept as its own module, independent of ``SearchCandidate``, so the
formula and its budgets are trivially unit-testable against plain
integers/floats and never need to import ``merger.py`` (which itself
imports these constants) -- avoids a circular import between the two.
"""

from __future__ import annotations

# Reciprocal Rank Fusion's own smoothing constant: ``1 / (k + rank)``.
# 60 is the plan's suggested starting point (also the value most
# commonly cited in RRF literature/benchmarks) -- named here rather than
# inlined so a later phase can retune it in one place. Larger `k` flattens
# the score curve (rank 1 vs. rank 20 matters less); smaller `k` rewards a
# top rank more steeply.
RRF_K = 60

# Candidate budgets (plan's recommended starting point): how many of
# each signal's already-ranked results ``merger.merge`` folds into
# candidates for fusion, and how large the resulting hybrid-tier pool is
# allowed to grow before ``reranker.rerank`` applies its final ``limit``.
# Deliberately separate from ``lexical.DEFAULT_LIMIT``/``semantic.
# DEFAULT_LIMIT``/``SearchConfig.semantic_top_k`` (a display/ANN-query
# budget, not a fusion one) -- RRF needs a genuine rank *within* each
# signal's own candidate pool, wider than what a caller may have asked
# to see, or a strong signal ranked just outside a small ``--limit``
# would never get the chance to fuse at all.
MAX_LEXICAL_CANDIDATES = 50
MAX_SEMANTIC_CANDIDATES = 50
MAX_FUSION_CANDIDATES = 100


def rrf_score(lexical_rank: int | None, semantic_rank: int | None, *, k: int = RRF_K) -> float:
    """``1 / (k + lexical_rank) + 1 / (k + semantic_rank)``, each term
    included only when that rank is known -- a candidate found by just
    one signal still gets a valid score from that term alone (the
    other's contribution is 0, not a penalty and not a reason to drop
    the candidate; see ``merger.py``'s ``SearchCandidate`` docstring).

    Ranks are 1-based (the best result in a signal's list has rank 1);
    ``None`` means "not found by this signal at all", never rank 0.
    """
    score = 0.0
    if lexical_rank is not None:
        score += 1.0 / (k + lexical_rank)
    if semantic_rank is not None:
        score += 1.0 / (k + semantic_rank)
    return score
