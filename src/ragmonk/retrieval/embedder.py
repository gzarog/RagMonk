"""Local sentence embeddings for Phase 9's real semantic search.

Uses ``sentence-transformers/all-MiniLM-L6-v2`` through plain
``transformers``/``torch`` calls (mean-pooling + L2-normalizing the last
hidden state by hand) rather than adding the ``sentence-transformers``
package on top. Both ``torch`` and ``transformers`` are already required,
non-optional dependencies via Docling's own PDF pipeline (see
``documents/docling_adapter.py`` and ``pyproject.toml``); for exactly one
fixed, well-known model, everything ``sentence-transformers`` would add
is ~15 lines of mean-pooling here (its own model card documents this
same snippet as the library-free equivalent), whereas the package itself
pulls in its own dependency tree (scikit-learn, scipy, Pillow, tqdm) for
no benefit this project needs. Same "biggest transitive win, smallest
add-on" call Phase 3 made for Docling's OCR/VLM extras.

Model identity + dimensionality (blueprint section 53) are fixed
constants, not user-configurable in this phase: ``EMBEDDING_MODEL_ID`` is
stamped onto every stored row (``storage/repositories/embeddings_repo.py``)
precisely so a *future* configurable model never gets silently mixed into
one similarity search -- see that module and ``retrieval/semantic.py``.

Runs entirely offline once ``EMBEDDING_MODEL_ID``'s weights are cached
locally (``huggingface_hub``'s default cache, respecting ``HF_HOME`` like
Docling's own model downloads already do -- Phase 3 set no precedent for
overriding that, so Phase 9 doesn't invent one either). The first call in
a fresh environment does need network access to populate that cache; see
the ``embedding_model`` pytest marker (``tests/unit/test_embedder.py``,
CONTRIBUTING.md) for how the default test suite avoids depending on it.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_MAX_TOKENS = 256
_BATCH_SIZE = 16
_MAX_CHARS = 4000  # defensive pre-tokenizer cap; a handful of huge inputs must not stall a batch


class EmbeddingModelUnavailableError(Exception):
    """The embedding model could not be loaded: ``torch``/``transformers``
    missing, no cached weights and no network, a corrupt cache, or any
    other Hugging Face Hub/local-load failure. A specific, catchable type
    (never a bare ``ImportError`` or library-specific exception) so
    callers (``retrieval/semantic.py``, ``indexing/embedding_indexer.py``)
    can degrade to "semantic search unavailable" instead of letting
    ``explore``/``search``/``index`` crash -- the blueprint's AI-optional
    principle extended to embeddings themselves.
    """


_lock = threading.Lock()
_handle: tuple[Any, Any] | None = None  # (tokenizer, model), loaded at most once per process


def _load_model() -> tuple[Any, Any]:
    global _handle
    with _lock:
        if _handle is not None:
            return _handle
        try:
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise EmbeddingModelUnavailableError(
                f"transformers/torch is not installed: {exc}"
            ) from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(EMBEDDING_MODEL_ID)
            model = AutoModel.from_pretrained(EMBEDDING_MODEL_ID)
        except Exception as exc:  # noqa: BLE001 - any HF Hub/cache failure -> unavailable
            raise EmbeddingModelUnavailableError(
                f"failed to load embedding model {EMBEDDING_MODEL_ID!r}: {exc}"
            ) from exc
        model.eval()
        _handle = (tokenizer, model)
        return _handle


def _mean_pool(last_hidden_state: Any, attention_mask: Any) -> Any:
    import torch

    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).to(last_hidden_state.dtype)
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """One L2-normalized, ``EMBEDDING_DIM``-length vector per input text,
    in the same order, batched ``_BATCH_SIZE`` at a time.

    Raises ``EmbeddingModelUnavailableError`` if the model cannot be
    loaded at all; never partially returns vectors for some texts and not
    others.
    """
    if not texts:
        return []
    tokenizer, model = _load_model()
    import torch

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = [text[:_MAX_CHARS] for text in texts[start : start + _BATCH_SIZE]]
        encoded = tokenizer(
            batch, padding=True, truncation=True, max_length=_MAX_TOKENS, return_tensors="pt"
        )
        with torch.no_grad():
            output = model(**encoded)
        pooled = _mean_pool(output.last_hidden_state, encoded["attention_mask"])
        normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
        vectors.extend(normalized.tolist())
    return vectors
