# ADR 0016: Lexical and hybrid search (RUST-10, slice 1)

Status: accepted

## Decisions (user)

1. RUST-10 ships in **three slices**:
   1. lexical + hybrid (this slice);
   2. symbol search and graph queries;
   3. explore/impact aggregation and attachment provenance.
2. RUST-09 slice 3 merged first, so this slice wires in its reranker.

## What this slice delivers

New crate `ragmonk-retrieval`, plus storage projections in
`ragmonk_storage::search`.

### Lexical search (`lexical`)

A port of `retrieval/lexical.py`:

- **Rank tiers:** exact symbol > qualified symbol > alias > title/heading
  > FTS > path.
- **Query-structure tiers** (`LexicalTier`): phrase > all terms > prefix
  > OR fallback. The plan runs most precise first and stops once `limit`
  distinct rows were seen; repeated expressions are skipped.
- **Tie-breaks:** FTS ordinal, entity kind, recency, source, path, id.
- **`(kind, id)` merge** keeps each result's best-ranked hit.
- **Matching:**
  - Tokens follow Python's Unicode `\w`.
  - Alias lookups match a dotted query against the last two segments of
    qualified names with at least three segments, without needing a
    schema column.
  - Chunk FTS uses the reference's BM25 column weights (heading 5, body 1,
    title 8) and match-centred `snippet()`.
  - Path search ANDs tokens, falling back to a substring scan.

### Hybrid search (`hybrid`)

A port of `merger.py` + `reranker.py` + `neural_reranker.rerank_hits`:

- **Merge.** Lexical and semantic candidates are deduplicated per
  `(kind, id)`, keeping both ranks. Each input is capped at its fusion
  budget (50/50, pool 100).
- **Pinned tier.** Exact, qualified, alias and title/heading matches always
  rank first and are never fused.
- **Hybrid tier.** Everything else is ordered by RRF (k = 60).
- **Neural pass (optional).** The cross-encoder from ADR 0015 rescores the
  top `search.reranker.top_n` hits.
- **Degradation.** Without the embedding model, fusion is lexical-only and
  `SemanticOutcome` reports why. Without the reranker, the RRF order is
  kept. Neither case is an error.
- **Multiple sources.** Multi-source search takes a list of `Corpus`
  (store + build).

### Indexing corrections

Found by the parity work, these make Rust's FTS content match the
reference:

- A heading chunk is indexed under its own text. Before, it was indexed
  under its parent heading path, which wrongly lost the title/heading tier
  for heading queries.
- A code entity without a signature is indexed under its name. Before, it
  was indexed under an empty string, which skewed BM25 for modules.

- `CODE_DERIVATION_VERSION` is bumped to `rust-code-2`, so code is
  re-derived.
- `CHUNKER_VERSION` stays at 2. It is part of the version stamp checked
  against the reference, so it cannot change on its own. V2 is unreleased,
  and the cutover (RUST-15/16) rebuilds every index from scratch, so no
  deployed V2 chunk index carries the old heading rows.

## Evidence

`compat/golden/search.json` comes from the reference
(`compat/tools/gen_search_golden.py`). It covers 105 queries:
- the reference's 72 search-quality golden queries, over its fixture
  project;
- 33 edge cases over the linking corpus: aliases, headings, case-folded
  titles, path fragments, mid-word fragments, punctuation, FTS operators,
  non-ASCII and token-less queries.

### How results are compared

- **Pinned mtimes.** Both sides index with every file mtime pinned, so
  recency tie-breaks are reproducible.
- **Tie keys.** Each expected hit carries its tie key: the reference's
  sort key without ids. Only hits with equal tie keys may appear in
  either order.
- **Deterministic FTS ties.** Rows with equal BM25 come back in insertion
  order. In the reference that follows its unsorted directory walk, and in
  V2 it follows worker scheduling, so it differs between machines. V2 breaks
  such ties by path, then qualified name and line (code) or chunk order
  (documents). The generator applies the same order to the reference
  queries.
- **Exact vector search.** The generator searches the reference's usearch
  index exactly. Its default approximate search dropped a vector in one
  build, and its brute-force fallback, unlike usearch, discards negative
  similarities.

### Results

| View | Matches the reference |
|---|---|
| Lexical (top 20) | 105 / 105 |
| Hybrid RRF (top 20) | 105 / 105 |
| Hybrid + cross-encoder | 105 / 105 (path hits compared as a set, see below) |

**Known difference: path-hit titles.** The reference titles a path hit with
its *absolute* path. V2 uses source-relative paths everywhere (ADR 0006).
Because the cross-encoder scores that title, path hits can land in
different positions after the neural pass. Every other hit keeps the
reference order.

## Benchmark

72 queries × 5 rounds, same machine, same corpus:

| | Python | Rust |
|---|---|---|
| Lexical p50 | 0.94 ms | 0.75 ms |
| Lexical p95 | 1.66 ms | 1.28 ms |

## Not in this slice

- **Slice 2:** symbol search, graph neighbours, callers, callees and
  references.
- **Slice 3:** explore/impact aggregation and attachment-location
  rendering.
- **RUST-12:** the CLI `search` command and output modes, including
  expanded chunk context.
- **RUST-13:** MCP.
- **Not ported:** the reference's in-process result cache. It only pays
  off in a long-lived server, so it is revisited with the daemon (RUST-11).
