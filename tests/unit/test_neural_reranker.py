"""``retrieval/neural_reranker.py``. Config gating, top-N selection,
batching/ordering, and graceful-fallback logic all run with a fake/stub
``score_batch`` in the default suite (no model load involved); tests that
actually load ``cross-encoder/ms-marco-MiniLM-L-6-v2`` are marked
``@pytest.mark.reranker_model`` (network + weight download on first use)
and excluded from the default run -- see CONTRIBUTING.md and
``pyproject.toml``'s ``addopts``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from ragpilot.retrieval import neural_reranker
from ragpilot.retrieval.lexical import RankTier, SearchResult
from ragpilot.retrieval.merger import merge
from ragpilot.retrieval.reranker import RankedHit, rerank


def _lexical(id_: str, tier: RankTier = RankTier.FTS, snippet: str = "") -> SearchResult:
    return SearchResult(
        kind="document",
        tier=tier,
        id=id_,
        title=id_,
        path=f"/{id_}",
        source_id="s1",
        snippet=snippet,
    )


def _ranked_hits(ids: Sequence[str]) -> list[RankedHit]:
    """A plain, already RRF-ordered ``RankedHit`` list for ``ids``, in
    that exact order (each found at a distinct, increasing lexical rank
    so their relative RRF order is unambiguous).
    """
    candidates = merge([_lexical(id_) for id_ in ids], [])
    by_id = {c.id: c for c in candidates}
    ordered = [by_id[id_] for id_ in ids]
    return rerank(ordered, limit=len(ordered))


def _reverse_score_batch(_query: str, texts: Sequence[str]) -> list[float]:
    """A deterministic stub scorer: highest score to the *last* text, so
    reordering by it exactly reverses input order -- makes reordering
    trivially observable without any real model.
    """
    return list(range(len(texts)))


# --- constants / error type ----------------------------------------------


def test_reranker_model_id_is_a_stable_constant() -> None:
    assert neural_reranker.RERANKER_MODEL_ID == "cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_neural_reranker_unavailable_error_is_a_plain_exception_type() -> None:
    assert issubclass(neural_reranker.NeuralRerankerUnavailableError, Exception)


def test_score_pairs_of_empty_list_is_empty_without_loading_a_model() -> None:
    assert neural_reranker.score_pairs("query", []) == []


# --- rerank_hits: no-op / gating cases -------------------------------------


def test_rerank_hits_is_a_noop_for_top_n_zero() -> None:
    hits = _ranked_hits(["a", "b", "c"])
    result = neural_reranker.rerank_hits("q", hits, top_n=0, score_batch=_reverse_score_batch)
    assert [h.candidate.id for h in result] == ["a", "b", "c"]


def test_rerank_hits_is_a_noop_for_fewer_than_two_hits() -> None:
    hits = _ranked_hits(["a"])
    result = neural_reranker.rerank_hits("q", hits, top_n=20, score_batch=_reverse_score_batch)
    assert [h.candidate.id for h in result] == ["a"]


def test_rerank_hits_is_a_noop_for_empty_input() -> None:
    result = neural_reranker.rerank_hits("q", [], top_n=20, score_batch=_reverse_score_batch)
    assert result == []


# --- rerank_hits: batching / ordering --------------------------------------


def test_rerank_hits_reorders_by_stub_score_descending() -> None:
    hits = _ranked_hits(["a", "b", "c"])
    result = neural_reranker.rerank_hits("q", hits, top_n=3, score_batch=_reverse_score_batch)
    # _reverse_score_batch scores the last text highest -- output is the
    # exact reverse of the RRF input order.
    assert [h.candidate.id for h in result] == ["c", "b", "a"]


def test_rerank_hits_only_rescores_the_top_n_prefix() -> None:
    """The remainder beyond ``top_n`` is never sent to the scorer at all,
    and stays in its original RRF order -- proven by a stub that records
    exactly which texts it was asked to score.
    """
    hits = _ranked_hits(["a", "b", "c", "d", "e"])
    seen: list[str] = []

    def _recording_score_batch(_query: str, texts: Sequence[str]) -> list[float]:
        seen.extend(texts)
        return list(range(len(texts)))

    result = neural_reranker.rerank_hits(
        "q", hits, top_n=2, score_batch=_recording_score_batch
    )
    assert seen == ["a", "b"]
    # Top 2 reversed by score, remainder (c, d, e) untouched and appended.
    assert [h.candidate.id for h in result] == ["b", "a", "c", "d", "e"]


def test_rerank_hits_passes_query_and_snippet_text_to_the_scorer() -> None:
    hits = _ranked_hits(["a"]) + _ranked_hits(["b"])
    # Give "a" a real snippet, "b" none -- _hit_text should fall back to
    # title for "b".
    hits[0] = replace(hits[0], candidate=replace(hits[0].candidate, snippet="a real snippet"))
    captured: dict[str, object] = {}

    def _capturing_score_batch(query: str, texts: Sequence[str]) -> list[float]:
        captured["query"] = query
        captured["texts"] = list(texts)
        return [1.0] * len(texts)

    neural_reranker.rerank_hits("my query", hits, top_n=5, score_batch=_capturing_score_batch)
    assert captured["query"] == "my query"
    assert captured["texts"] == ["a real snippet", "b"]


def test_rerank_hits_is_stable_for_tied_scores() -> None:
    hits = _ranked_hits(["a", "b", "c"])

    def _all_equal_score_batch(_query: str, texts: Sequence[str]) -> list[float]:
        return [1.0] * len(texts)

    result = neural_reranker.rerank_hits(
        "q", hits, top_n=3, score_batch=_all_equal_score_batch
    )
    assert [h.candidate.id for h in result] == ["a", "b", "c"]


# --- rerank_hits: graceful fallback ----------------------------------------


def test_rerank_hits_falls_back_to_original_order_when_model_unavailable() -> None:
    hits = _ranked_hits(["a", "b", "c"])

    def _unavailable_score_batch(_query: str, _texts: Sequence[str]) -> list[float]:
        raise neural_reranker.NeuralRerankerUnavailableError("no network, no cached weights")

    result = neural_reranker.rerank_hits(
        "q", hits, top_n=3, score_batch=_unavailable_score_batch
    )
    assert [h.candidate.id for h in result] == ["a", "b", "c"]


def test_rerank_hits_falls_back_when_score_batch_returns_a_mismatched_count() -> None:
    hits = _ranked_hits(["a", "b", "c"])

    def _short_score_batch(_query: str, _texts: Sequence[str]) -> list[float]:
        return [1.0]  # one score for three candidates -- must not silently zip-truncate

    result = neural_reranker.rerank_hits("q", hits, top_n=3, score_batch=_short_score_batch)
    assert [h.candidate.id for h in result] == ["a", "b", "c"]


def test_rerank_hits_never_raises_the_unavailable_error_itself() -> None:
    hits = _ranked_hits(["a", "b"])

    def _boom(_query: str, _texts: Sequence[str]) -> list[float]:
        raise neural_reranker.NeuralRerankerUnavailableError("boom")

    try:
        neural_reranker.rerank_hits("q", hits, top_n=2, score_batch=_boom)
    except neural_reranker.NeuralRerankerUnavailableError:
        pytest.fail("rerank_hits must catch NeuralRerankerUnavailableError, never propagate it")


# --- real model (excluded from the default suite) --------------------------


@pytest.mark.reranker_model
def test_score_pairs_ranks_a_relevant_passage_above_an_irrelevant_one() -> None:
    scores = neural_reranker.score_pairs(
        "What is the capital of France?",
        [
            "Paris is the capital and most populous city of France.",
            "Bananas are a good source of potassium.",
        ],
    )
    assert len(scores) == 2
    assert scores[0] > scores[1]


@pytest.mark.reranker_model
def test_rerank_hits_with_the_real_model_reorders_by_relevance() -> None:
    candidates = merge(
        [
            _lexical("off_topic", snippet="Bananas are a good source of potassium."),
            _lexical("on_topic", snippet="Paris is the capital and most populous city of France."),
        ],
        [],
    )
    hits = rerank(candidates, limit=2)
    result = neural_reranker.rerank_hits(
        "What is the capital of France?", hits, top_n=2
    )
    assert result[0].candidate.id == "on_topic"
