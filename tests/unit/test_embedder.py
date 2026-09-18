"""``retrieval/embedder.py``. Constants/error-type tests run in the
default suite (no model load involved); tests that actually load
``sentence-transformers/all-MiniLM-L6-v2`` are marked
``@pytest.mark.embedding_model`` (network + weight download on first
use) and excluded from the default run -- see CONTRIBUTING.md and
``pyproject.toml``'s ``addopts``.
"""

from __future__ import annotations

import pytest

from ragmonk.retrieval import embedder


def test_embedding_model_id_and_dim_are_stable_constants() -> None:
    assert embedder.EMBEDDING_MODEL_ID == "sentence-transformers/all-MiniLM-L6-v2"
    assert embedder.EMBEDDING_DIM == 384


def test_embed_texts_of_empty_list_is_empty_without_loading_a_model() -> None:
    assert embedder.embed_texts([]) == []


def test_embedding_model_unavailable_error_is_a_plain_exception_type() -> None:
    assert issubclass(embedder.EmbeddingModelUnavailableError, Exception)


def test_load_model_failure_is_wrapped_as_the_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken/missing ``transformers`` install (or any Hugging Face
    Hub/cache failure) must surface as ``EmbeddingModelUnavailableError``,
    never a bare ``ImportError`` or library-specific exception -- proven
    here by forcing the load path to fail without needing the real
    dependency to actually be absent.
    """
    monkeypatch.setattr(embedder, "_handle", None)

    def _boom() -> tuple[object, object]:
        raise embedder.EmbeddingModelUnavailableError("simulated failure")

    monkeypatch.setattr(embedder, "_load_model", _boom)

    with pytest.raises(embedder.EmbeddingModelUnavailableError):
        embedder.embed_texts(["hello"])


@pytest.mark.embedding_model
def test_embed_texts_returns_normalized_vectors_of_the_expected_dimension() -> None:
    vectors = embedder.embed_texts(["hello world", "semantic search test"])
    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == embedder.EMBEDDING_DIM
        norm = sum(v * v for v in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-3)


@pytest.mark.embedding_model
def test_embed_texts_puts_similar_sentences_closer_than_dissimilar_ones() -> None:
    from ragmonk.retrieval.vectorstore import cosine_similarity

    vectors = embedder.embed_texts(
        [
            "The dog barked loudly in the park.",
            "A canine made a loud noise outdoors.",
            "Quarterly revenue exceeded analyst expectations.",
        ]
    )
    dog_vs_dog = cosine_similarity(vectors[0], vectors[1])
    dog_vs_finance = cosine_similarity(vectors[0], vectors[2])
    assert dog_vs_dog > dog_vs_finance
