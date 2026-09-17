# RagMonk search quality baseline (Phase 0)

Generated: 2026-09-17T11:48:47Z

## Golden-query quality (overall)

- Recall@1: 0.7407
- Recall@3: 0.9051
- Recall@5: 0.9606
- Recall@10: 1.0
- MRR: 0.9028
- NDCG@10: 0.9158
- Queries evaluated: 72

## Golden-query quality (by category)

| Category | N | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| code_to_document | 5 | 0.0 | 0.8 | 0.8 | 1.0 | 0.5 | 0.6207 |
| cross_document | 8 | 0.4167 | 0.8333 | 0.8958 | 1.0 | 0.9375 | 0.9085 |
| exact_heading_lookup | 6 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| exact_symbol_lookup | 11 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| exact_title_lookup | 6 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| file_path_lookup | 4 | 0.375 | 0.375 | 0.75 | 1.0 | 0.8125 | 0.736 |
| keyword_search | 8 | 0.8125 | 1.0 | 1.0 | 1.0 | 0.9375 | 0.9539 |
| semantic_document | 8 | 0.625 | 0.75 | 1.0 | 1.0 | 0.7812 | 0.8416 |
| table_question | 8 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| typo_partial_term | 8 | 0.75 | 1.0 | 1.0 | 1.0 | 0.875 | 0.9077 |

## Latency (synthetic corpus)

Corpus size: small, repeats: 20

- lexical: p50=1.827 ms, p95=7.398 ms
- semantic: p50=1.024 ms, p95=1.094 ms
- hybrid: p50=12.941 ms, p95=14.031 ms

## Cold vs warm semantic search

- Cold (first call, loads ANN index from disk): 3.875 ms
- Warm p50: 1.097 ms, p95: 1.196 ms

## Indexing / storage

- Indexing time by file type: code=42.81 ms, document=99.746 ms
- Full index time (mixed project): 201.366 ms
- Re-index time (no changes): 54.024 ms
- Generated chunk count: 26 (entities=10, document_sections=16)
- Vector count: 26
- Knowledge DB size: 335872 bytes
- Vector index size: 7388 bytes

> Measured against the small, hand-written fixture project (a handful of files) -- real, honestly-obtained numbers, but indicative of pipeline correctness/overhead only, not representative of large-corpus performance. See the latency section for synthetic-corpus numbers.
