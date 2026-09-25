"""A fast, deterministic, fully offline stand-in for ``retrieval/
embedder.py``'s real ``embed_texts`` (network fetch + ML inference) --
the same hash-based technique this project's own test suite already
uses for semantic-retrieval tests (see ``tests/integration/
test_semantic_retrieval.py``).

The benchmark suite measures *latency*, not relevance quality (that is
what the separate golden-query quality regression tests are for), so a
query embedding here only needs to be fast, deterministic, and the
right dimensionality to exercise the real ANN/cosine-similarity code
paths -- it does not need real semantic meaning.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from benchmarks.search.corpus import EMBEDDING_DIM


def fake_embed_texts(texts: Sequence[str], *, batch_size: int | None = None) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.lower().encode("utf-8")).digest()
        raw = [b / 255.0 for b in digest[:EMBEDDING_DIM]]
        norm = sum(v * v for v in raw) ** 0.5 or 1.0
        vectors.append([v / norm for v in raw])
    return vectors
