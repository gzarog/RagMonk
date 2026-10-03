"""Generate the Python reference vectors for RUST-09 embedding parity.

Runs the V1 embedder (``ragmonk.retrieval.embedder.embed_texts``) over
``compat/fixtures/embeddings/texts.json`` and writes the vectors, rounded to
6 decimals, to ``compat/golden/embeddings-minilm.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ragmonk.retrieval.embedder import EMBEDDING_MODEL_ID, embed_texts

ROOT = Path(__file__).resolve().parents[1]
TEXTS = ROOT / "fixtures" / "embeddings" / "texts.json"
OUT = ROOT / "golden" / "embeddings-minilm.json"


def main() -> None:
    texts = json.loads(TEXTS.read_text(encoding="utf-8"))["texts"]
    vectors = embed_texts(texts)
    payload = {
        "model": EMBEDDING_MODEL_ID,
        "vectors": [[round(v, 6) for v in vec] for vec in vectors],
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(vectors)} vectors)")


if __name__ == "__main__":
    main()
