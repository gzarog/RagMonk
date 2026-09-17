"""``retrieval/vectorstore.py``'s brute-force cosine similarity: pure
Python, precomputed vectors, no embedding model -- see that module's
docstring for why this is Phase 9's chosen "equivalent embedded index"
over sqlite-vec.
"""

from __future__ import annotations

import pytest

from ragmonk.retrieval.vectorstore import cosine_similarity, top_k


def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors_is_minus_one() -> None:
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_mismatched_length_is_zero_not_an_error() -> None:
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_similarity_zero_vector_is_zero_not_an_error() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_empty_vector_is_zero() -> None:
    assert cosine_similarity([], []) == 0.0


def test_top_k_ranks_by_similarity_descending() -> None:
    query = [1.0, 0.0]
    candidates = [
        ("far", [0.0, 1.0]),
        ("close", [1.0, 0.01]),
        ("exact", [1.0, 0.0]),
    ]
    ranked = top_k(query, candidates, k=3)
    assert [c.key for c in ranked] == ["exact", "close", "far"]
    assert ranked[0].score == pytest.approx(1.0)


def test_top_k_respects_k() -> None:
    query = [1.0, 0.0]
    candidates = [(str(i), [1.0, float(i)]) for i in range(10)]
    ranked = top_k(query, candidates, k=3)
    assert len(ranked) == 3


def test_top_k_filters_below_min_score() -> None:
    query = [1.0, 0.0]
    candidates = [("match", [1.0, 0.0]), ("orthogonal", [0.0, 1.0])]
    ranked = top_k(query, candidates, k=10, min_score=0.5)
    assert [c.key for c in ranked] == ["match"]


def test_top_k_ties_break_deterministically_by_key() -> None:
    query = [1.0, 0.0]
    candidates = [("b", [1.0, 0.0]), ("a", [1.0, 0.0])]
    ranked = top_k(query, candidates, k=2)
    assert [c.key for c in ranked] == ["a", "b"]
