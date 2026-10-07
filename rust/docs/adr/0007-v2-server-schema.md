# ADR 0007 — V2 OpenSearch/Elasticsearch schema and legacy cleanup

Status: accepted (RUST-03)

## Decision
* **Prefix.** V2 indexes live under `{storage.server.index_prefix}-v2`
  (default `ragmonk-v2`), so they can never collide with V1 names.
* **Six specialized indexes**: `-source-state`, `-files`, `-code`,
  `-documents` (top-level documents and EML attachment children with
  `parent_document_id`/`attachment_*` provenance), `-chunks` (retrieval
  text and vectors) and `-relationships` (code edges and cross-domain
  links, distinguished by `record_kind`). Mappings are `dynamic: strict`;
  bulky text (`text`, `embedding_text`, `evidence`, `table_rows`) is stored
  but not indexed.
* **Schema identity** is stored in each index's `_meta` (`schema`,
  `schema_version`, vector spec). `init` creates missing indexes, waits for
  primaries, and verifies existing ones; any mismatch is refused — indexes
  are never mutated in place. A schema change means a new V2 index set and a
  full rebuild.
* **Vectors** are defined at creation (`knn_vector`+HNSW/lucene/cosine on
  OpenSearch, `dense_vector`+HNSW/cosine on Elasticsearch). Until RUST-09
  pins the V2 model, the provisional spec is 384 dims (current reference
  model), m=16, ef_construction=128.
* **IDs.** Records use `ragmonk_core::ids::v2` IDs; the document `_id` is
  `<build_id>:<record id>` — deterministic and idempotent within V2, and
  builds never overwrite each other.
* **Atomic publication.** A source-state document holds `active_build_id`
  and `pending_build_id`. Publishing refreshes the build-scoped indexes and
  switches the active build with an optimistic-concurrency write
  (`if_seq_no`/`if_primary_term`; a concurrent publisher gets a conflict),
  then deletes every other build of the source. Every read filters on
  `(source_id, active_build_id)` pairs; with no published build nothing
  matches. Starting a new build deletes the leftovers of an abandoned one;
  aborting keeps the previous build visible.
* **Bulk writes** never exceed `storage.server.bulk.max_actions` or
  `max_bytes` per request, shrink on 413/429, grow back on success, retry
  only 429/503 items with backoff up to `max_retries`, and treat a delete of
  a missing document as success. Request telemetry counts requests per
  endpoint, bytes and bulk actions; the live tests assert every record write
  goes through `_bulk`.
* **Build mode** (`enter_build_mode`) sets `refresh_interval: -1` and zero
  replicas on build-scoped indexes for a large rebuild and restores the
  saved serving settings explicitly or on drop.
* **Legacy cleanup.** `ragmonk server-v2 legacy` lists the exact V1 names
  (`{p}-files|content|relationships` for the configured prefix and the
  historical prefixes in `legacy::LEGACY_PREFIXES`) with doc counts and a
  fingerprint. `--delete --confirm <fingerprint>` deletes those exact names
  only if the set is unchanged. Pattern deletes are never used and nothing
  else is touched. Per the project decision (ADR 0006) no index-level
  rollback is kept; RUST-15 folds this into `migrate-to-rust-v2 --execute`.

## Evidence
`crates/ragmonk-backends/tests/live.rs` runs the full lifecycle against real
OpenSearch 2.15 and Elasticsearch 8.15 (CI job "Rust V2 server
integration"), including injected crashes/aborts and visibility checks.
