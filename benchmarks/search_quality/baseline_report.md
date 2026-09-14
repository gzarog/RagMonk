# RAGpilot search quality baseline (Phase 0)

Generated: 2026-09-14T11:13:53Z

## Golden-query quality (overall)

- Recall@1: 0.6713
- Recall@3: 0.8843
- Recall@5: 0.9653
- Recall@10: 1.0
- MRR: 0.8542
- NDCG@10: 0.8833
- Queries evaluated: 72

## Golden-query quality (by category)

| Category | N | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| code_to_document | 5 | 0.0 | 0.8 | 0.8 | 1.0 | 0.5 | 0.6207 |
| cross_document | 8 | 0.1667 | 0.6458 | 0.9375 | 1.0 | 0.6875 | 0.7543 |
| exact_heading_lookup | 6 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| exact_symbol_lookup | 11 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| exact_title_lookup | 6 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| file_path_lookup | 4 | 0.375 | 0.375 | 0.75 | 1.0 | 0.8125 | 0.736 |
| keyword_search | 8 | 0.5625 | 1.0 | 1.0 | 1.0 | 0.8125 | 0.8616 |
| semantic_document | 8 | 0.625 | 0.75 | 1.0 | 1.0 | 0.7812 | 0.8416 |
| table_question | 8 | 0.875 | 1.0 | 1.0 | 1.0 | 0.9375 | 0.9539 |
| typo_partial_term | 8 | 0.75 | 1.0 | 1.0 | 1.0 | 0.875 | 0.9077 |

## Latency (synthetic corpus)

Corpus size: small, repeats: 20

- lexical: p50=3.337 ms, p95=12.764 ms
- semantic: p50=1.263 ms, p95=1.395 ms
- hybrid: p50=11.657 ms, p95=12.691 ms

## Cold vs warm semantic search

- Cold (first call, loads ANN index from disk): 3.745 ms
- Warm p50: 1.215 ms, p95: 1.371 ms

## Indexing / storage

- Indexing time by file type: code=37.016 ms, document=44.143 ms
- Full index time (mixed project): 66.54 ms
- Re-index time (no changes): 34.151 ms
- Generated chunk count: 26 (entities=10, document_sections=16)
- Vector count: 26
- Knowledge DB size: 331776 bytes
- Vector index size: 7388 bytes

> Measured against the small, hand-written fixture project (a handful of files) -- real, honestly-obtained numbers, but indicative of pipeline correctness/overhead only, not representative of large-corpus performance. See the latency section for synthetic-corpus numbers.
