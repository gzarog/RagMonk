"""Generate the Python reference scores for RUST-09 reranker parity.

Runs the V1 cross-encoder (``ragmonk.retrieval.neural_reranker.score_pairs``)
for every query in ``compat/fixtures/embeddings/queries.json`` against every
passage in ``compat/fixtures/embeddings/texts.json``. The passages include
over-length texts (pair truncation), Greek, Japanese and emoji. Logits,
rounded to 5 decimals, go to ``compat/golden/reranker-ms-marco.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ragmonk.retrieval.neural_reranker import RERANKER_MODEL_ID, score_pairs

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "embeddings"
OUT = ROOT / "golden" / "reranker-ms-marco.json"


def main() -> None:
    texts = json.loads((FIXTURES / "texts.json").read_text(encoding="utf-8"))["texts"]
    queries = json.loads((FIXTURES / "queries.json").read_text(encoding="utf-8"))["queries"]
    scores = [[round(s, 5) for s in score_pairs(q, texts)] for q in queries]
    payload = {"model": RERANKER_MODEL_ID, "queries": queries, "scores": scores}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(queries)} x {len(texts)} pairs)")


if __name__ == "__main__":
    main()
