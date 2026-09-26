# Indexing performance benchmarks

`benchmarks/indexing/` is a reproducible, fully-offline benchmark suite
for RagMonk's indexing pipeline, built as Phase P0 of the indexing
performance optimization plan (`ragmonk-indexing-performance-v1`, see
`CHANGELOG.md`'s Unreleased section). It exists so every later
optimization phase is validated against real, measured before/after
numbers instead of an assumed speedup.

## What it measures

For a generated synthetic corpus, the suite runs the exact production
indexing entry point (`ragmonk.indexing.runner.run_source_pass` — the
same code path `ragmonk index` and the daemon use) through a fixed
sequence of realistic scenarios:

| Scenario             | What it simulates                                  |
| --------------------- | --------------------------------------------------- |
| `cold_index`           | First-ever index of a brand new source               |
| `warm_unchanged`       | Re-running with nothing changed                      |
| `single_edit`          | One file edited                                      |
| `one_percent_change`   | ~1% of files edited                                  |
| `burst_change`         | ~5% of files edited (a burst of activity)            |
| `rename`               | One file moved/renamed                               |
| `delete`               | A small batch of files deleted                       |

For each scenario it records: wall time, files/sec, scan result counts
(new/changed/unchanged/deleted/moved/indexed/failed), the number and
total duration/bytes of `hash_file` calls (across every module that
calls it today — the coordinator, the document pipeline and the PDF
adapter independently, per finding F4), CPU time, peak RSS, and
correctness counts (entities, document sections) from the resulting
SQLite database.

Everything runs against **generated, synthetic content only** — code
files are small deterministic Python modules; document files are
repeated copies of the repository's own tiny, already-committed
`tests/fixtures/documents/*.{txt,md,html,csv}` samples. No confidential
or real-world data is ever generated, downloaded or committed.

## Running it

```bash
# from a venv with the project installed (`uv pip install -e ".[dev]"`)
python -m benchmarks.indexing --tier small
python -m benchmarks.indexing --tier medium --out /tmp/my_run.json
python -m benchmarks.indexing --tier large    # 10k files, several minutes
python -m benchmarks.indexing --tier scale    # 200k files, optional, slow
```

Tiers (`--tier`): `tiny` (50 files), `small` (500), `medium` (2,000),
`large` (10,000), `scale` (200,000, opt-in — not run by default or in
CI due to disk/time cost).

The run is fully offline: no network access, no cloud API keys. If
Docling/torch are not installed in the current environment, document
indexing is automatically disabled for the run
(`RAGMONK_DOCUMENTS__ENABLED=false`) and this is recorded in the output
JSON's `environment.docling_available` field rather than silently
skipped.

Output is a machine-readable JSON file (default
`benchmarks/indexing/baseline_<tier>.json`) with one record per
scenario plus an environment block (Python version, platform, CPU
count, RagMonk version, Docling availability) so results are
comparable across runs and machines.

## Recorded baselines

`benchmarks/indexing/baseline_small.json` and `baseline_medium.json` in
this directory are the **measured baseline** against the pre-P1
implementation (commit `d8a5fad`), captured on the CI/dev container this
work was done in. Re-run the suite yourself before trusting these
numbers on different hardware — they are not portable performance
guarantees, only a reproducible reference point for regression
comparison.

## Phase P7: before/after report

`benchmarks/indexing/after_p7_small.json` is the **measured "after"**
run, same `small` tier, same corpus generator, captured on the same
CI/dev container as `baseline_small.json` after every optimization
phase (P1–P6) plus one correctness fix P7 itself found and fixed (see
below) had landed.

| Scenario             | before: `changed` | after: `changed` | before: wall | after: wall |
| --------------------- | -----------------: | -----------------: | -------------: | ------------: |
| `cold_index`           |                  0 |                  0 |        28.4s   |       22.3s   |
| `warm_unchanged`       |                  0 |                  0 |        0.109s  |       0.071s  |
| `single_edit`          |                  1 |                  1 |        0.135s  |       0.119s  |
| `one_percent_change`   |                  6 |                  5 |        0.240s  |       0.156s  |
| `burst_change`         |                 31 |                 25 |        0.659s  |       0.302s  |
| `rename`               |             **31** |              **0** |    **0.583s**  |   **0.070s**  |
| `delete`               |             **31** |              **0** |    **0.774s**  |   **0.082s**  |

`hash_calls` moves in exact lockstep with `changed` in both runs (P3's
reuse already ensures one hash per genuinely changed file, never more)
— the story here is entirely in how many files `changed` should have
been, not in per-file hashing cost.

### The bug P7 found

Every phase before P7 validated its own change in isolation — one edit,
one rerun. Phase P7's job (this file's own `## Extending it` note,
below, always said P7 would run the full suite end-to-end) surfaced
something none of them could: **a genuinely pre-existing bug, present
in `baseline_small.json`'s own pre-P1 numbers above and therefore
predating this entire optimization plan.**

`IndexCoordinator`'s scan loop computed a changed file's new size/mtime/
content_hash correctly, but only ever persisted its `status` to the
`files` table — never the new stat. The next file to read that row
(`_process_queue`'s `files_repo.get()`) got back the *old* size/mtime/
content_hash, and `mark_indexed` dutifully wrote those stale values
straight back at the end. The practical effect: **every edited file
looked "changed" again on every subsequent `ragmonk index` run,
forever** — regardless of Phase P3's content-hash reuse or Phase P2's
targeted scanning, both of which were correctly detecting a real
mismatch against data that was itself wrong. The `rename`/`delete`
scenarios' `changed: 31` above (pre-fix) is this bug in action: files
"changed" by an earlier scenario in the same run never actually
cleared.

Fixed in `files_repo.update_status()` (now accepts optional `size`/
`mtime`/`content_hash`, `COALESCE`d against the stored value like
`mark_indexed`'s own version-stamp params) and its three call sites in
`indexing/coordinator.py` (the full-scan path and both targeted-scan
branches). See `tests/unit/test_index_coordinator_changed_file_stat_
persistence.py` and `tests/integration/test_indexing_optimization_
regression.py` for the regression coverage, and this file's own git
history for the full before/after story.

## Extending it

`fixtures.py` holds the corpus generator; `metrics.py` holds the
resource/hash-call instrumentation; `runner.py` drives one scenario
through the real indexing entry point. Later phases (P1–P7) should
extend this suite's scenarios rather than replace it — in particular,
P7 re-runs these exact fixtures against the optimized implementation
for the plan's required before/after report.

## Indexing optimization plan V2 / Completion (P6)

V2's own completion phase re-ran the same `small`/`medium` tiered suite
above (`benchmarks/indexing/after_v2_p6_small.json`,
`after_v2_p6_medium.json`) plus a new supplemental script
(`benchmarks/indexing/v2_supplemental.py` →
`v2_p6_supplemental.json`) covering scenarios the tiered suite doesn't:
document-heavy indexing, an embedding backfill, embedding-cache reuse
across two passes, and concurrent document extraction
(`indexing.document_extraction_workers > 1`).

`ScenarioMetrics` (`metrics.py`) grew new fields this phase: per-stage
durations (`scan_seconds`/`classify_seconds`/`process_seconds`/
`linking_seconds`/`embedding_seconds`/`ann_sync_seconds`, sourced from
V2 Phase P5's `IndexRunResult.timings`), `embedding_cache_reused` (V2
Phase P3), `code_extraction_workers`/`document_extraction_workers`, and
an optional `sql_statement_count` (via the new, opt-in
`storage.sqlite.count_statements` hook — V2 Phase P4).

### small tier: P0-P7 baseline vs. V2-P6 (same corpus generator, same container)

| Scenario             | P0-P7 `after_p7_small.json` wall | V2-P6 wall | changed (both) |
| --------------------- | -----------------------------: | -----------: | --------------: |
| `cold_index`           |                        22.328s |      23.743s |            0 |
| `warm_unchanged`       |                         0.071s |       0.074s |            0 |
| `single_edit`          |                         0.119s |       0.126s |            1 |
| `one_percent_change`   |                         0.156s |       0.148s |            5 |
| `burst_change`         |                         0.302s |       0.333s |           25 |
| `rename`               |                         0.070s |       0.100s |            0 |
| `delete`               |                         0.082s |       0.094s |            0 |

These numbers are **flat, not improved**, and that is expected, not a
regression: `search.semantic` is off by default in this suite (matching
this project's own default), so P3's embedding cache and P4's
embedding-write-path batching never engage at all — nothing in P1-P5
targets this suite's own default (non-semantic) code-indexing path.
P1's version-provider check is a cheap no-op comparison already covered
by the existing `_version_reprocess_decision` path P0-P7 already paid
for; P2's concurrency defaults to `workers=1` (byte-for-byte the serial
path); P5's telemetry measured separately below as zero-cost at the
default log level. The real point of this table is **no regression**:
every `changed` count matches exactly, and every wall time is within
the same rough envelope (a different container/session than the
original P0-P7 run, so small swings either way are expected background
noise, not a measured optimization or regression).

### medium tier: V2-P6 own run (`after_v2_p6_medium.json`, 2,100 files)

| Scenario             | wall     | changed |
| --------------------- | -------: | ------: |
| `cold_index`           | 293.372s |       0 |
| `warm_unchanged`       |   0.287s |       0 |
| `single_edit`          |   0.527s |       1 |
| `one_percent_change`   |   1.062s |      20 |
| `burst_change`         |   2.050s |     100 |
| `rename`               |   0.310s |       0 |
| `delete`               |   0.524s |       0 |

Required V2-P6 acceptance tier — no false deletions, no stale
generations (every `changed` count matches what the scenario itself
changed), no correctness regressions.

### Supplemental V2 scenarios (real, unmocked embedder/Docling; n=400 docs unless noted)

**`document_heavy`** (400 plain-text/HTML/CSV/Markdown documents, cold
index): 5.743s wall, 400/400 indexed, 0 failed.

**`embedding_backfill`** (`ragmonk vectors backfill`'s own real path —
files indexed while `search.semantic` was off, then backfilled): 400
files found missing embeddings, 1,200 subjects embedded, 0.540s wall.

**`embedding_cache_reuse`** (V2 Phase P3's own point — a vector rebuild
over unchanged content, same project, same model/preprocessing
identity): first pass 2.445s / 1,200 embedded / 0 cache hits (cold);
second pass **0.081s** / 1,200 embedded / **210 cache hits** — a
**~30x wall-time drop** for this specific rebuild-style re-embed, with
every subject still produced correctly (`embedded` count unchanged).
This is real, measured evidence for P3's stated goal: avoiding repeated
model inference across separate runs.

**`concurrent_document_extraction`** (`document_extraction_workers`
1 vs. 4, same 400-document corpus, real Docling conversion — plain-text/
HTML/CSV/Markdown backends only; no PDF/OCR model weights were cached in
this environment, see below):

| workers | wall_time_s | process_seconds |
| ------: | ----------: | ---------------: |
|       1 |      1.696s |            1.538s |
|       4 |      1.979s |            1.807s |

**`document_extraction_workers` stays at its default of `1`.** Measured
across two corpus sizes (n=100 and n=400 documents), `workers=4` was
consistently *slower* than `workers=1` for this project's rule-based
document backends — thread-pool/per-worker-connection overhead
outweighs the benefit when each individual conversion is already fast
(no ML inference involved for these formats). This directly supersedes
V2 Phase P2's own informal smoke test (which used artificially slowed,
mocked `prepare` functions and showed a large apparent speedup) with
real, unmocked evidence: for the formats measurable in this
environment, concurrency is not worth its own overhead, so the phase's
"serial until proven safe" default is kept, backed by this data rather
than left unchanged by default alone.

### What was not run, and why

- **10k/200k file tiers**: optional per the plan; not run in this pass
  due to the time budget for a single environment/session (the `medium`
  tier's `cold_index` alone took ~4.9 minutes single-threaded in this
  container). `python -m benchmarks.indexing --tier large`/`--tier
  scale` reproduce these on demand.
- **WSL-native vs. `/mnt/c` comparison**: not applicable — this
  environment is a Linux container, not WSL.
- **Network-share source scenario**: not run — no network filesystem
  available in this sandboxed environment.
- **Real PDF/OCR document conversion under concurrency**: not run.
  Docling's layout/table-structure model weights and RapidOCR's model
  weights are not cached in this environment (only the pinned embedding
  model's weights were available offline), and downloading them was
  judged out of scope for this pass's time budget. The
  `concurrent_document_extraction` result above is therefore proven only
  for Docling's rule-based backends (plain text/HTML/CSV/Markdown), not
  for the ML-inference-heavy PDF/image-OCR path P2's own design doc
  specifically worried about oversubscribing (`cap_native_thread_pools`)
  — this remains an explicitly disclosed, unverified gap.

## Real server-backend indexing benchmark (`benchmarks/server_indexing`)

`benchmarks/indexing/` above is deliberately fully offline and always
targets the local SQLite/FTS5/USearch backend. `benchmarks/server_indexing/`
is a **separate** benchmark that indexes a synthetic corpus against a REAL
running OpenSearch or Elasticsearch cluster, through the exact same
production entry points as `benchmarks/indexing/`
(`ragmonk.indexing.runner.run_source_pass`) and the real
`OpenSearchKnowledgeBackend`/`ElasticsearchKnowledgeBackend` adapters and
their real bulk-publish path (`opensearch_bulk`/`elasticsearch_bulk`) — no
second, parallel indexing path is built for this.

### What it generates

`benchmarks/server_indexing/corpus.py` generates a synthetic corpus of
Python and JavaScript modules (80/20 split by default) wired together with
real cross-file imports and calls: module `N` imports and calls a helper
from module `N-1`, forming a chain across the whole corpus, plus a small
fraction of Markdown docs referencing specific modules. This is what gives
a real indexing pass genuine cross-file references and a non-trivial
call graph to resolve and publish (symbol/entity extraction, linking), not
just N independent files.

### Running it

```bash
# Bring up real, disposable single-node clusters (dev-only; never required
# for RagMonk itself -- see docker/docker-compose.*.yml's own docstrings):
docker compose -f docker/docker-compose.opensearch.yml up -d
docker compose -f docker/docker-compose.elasticsearch.yml up -d

# OpenSearch, ~5,000 files, cold index + incremental update + lexical
# query latency, written to a JSON report:
python -m benchmarks.server_indexing --engine opensearch \
    --url http://localhost:9200 --num-files 5000 \
    --out benchmarks/server_indexing/opensearch_5000.json

# Elasticsearch, same shape:
python -m benchmarks.server_indexing --engine elasticsearch \
    --url http://localhost:9200 --num-files 5000 \
    --out benchmarks/server_indexing/elasticsearch_5000.json

# Also measure hybrid/semantic query latency (needs the local embedding
# model available -- off by default, matching this project's own default):
python -m benchmarks.server_indexing --engine opensearch \
    --url http://localhost:9200 --num-files 5000 --semantic

# Skip the incremental phase (cold index only):
python -m benchmarks.server_indexing --engine opensearch \
    --url http://localhost:9200 --num-files 5000 --no-incremental
```

With no reachable cluster at `--url` (or when `--harness-smoke-test` is
passed explicitly), the run still executes in full against whichever
backend it can reach, but the report is labeled
`"harness_smoke_test": true` with a `notes` entry explaining why, and
`"real_cluster": false` — those numbers demonstrate the benchmark harness
itself works, not a real-cluster result, and should never be quoted as
satisfying a "real 2,000+ file cluster run" requirement.

### What each metric in the JSON report means

Top level:

- `engine`: `"opensearch"` or `"elasticsearch"`.
- `real_cluster`: `true` only when a real, reachable cluster was
  validated at `--url` and used for the run.
- `harness_smoke_test`: `true` when `real_cluster` is `false` (or
  `--harness-smoke-test` was passed) -- see `notes` for why.
- `cold_index`: the first-ever indexing pass (see below).
- `incremental`: the second-pass, changed-corpus result (see below), or
  `null` if `--no-incremental` was passed.
- `lexical_latency` / `hybrid_latency`: query latency percentiles (see
  below); `hybrid_latency` is `null` unless `--semantic` was passed and
  the semantic search path was actually available.

Each `cold_index`/`incremental.index_metrics` block (an `IndexRunMetrics`):

- `files_scanned`/`files_new`/`files_changed`/`files_unchanged`/
  `files_deleted`/`files_moved`/`files_indexed`/`files_failed`: the real
  `IndexRunResult` counters from `run_source_pass` -- exactly what
  `ragmonk index` itself reports.
- `wall_time_s`: total wall-clock time for the pass.
- `scan_seconds`/`classify_seconds`/`process_seconds`/`linking_seconds`/
  `embedding_seconds`/`ann_sync_seconds`: the same per-stage timings
  `benchmarks/indexing` uses (`IndexRunResult.timings`); for a
  server-mode pass, `process_seconds` covers per-file
  parse/prepare **and** the real server publish/bulk-write calls
  together (the coordinator has no finer split between them today -- see
  `StageTimings`'s own docstring in `indexing/coordinator.py`).
- `files_per_sec`/`bytes_per_sec`: `files_scanned`/`total_bytes` divided
  by `wall_time_s`.
- `bulk_actions`/`bulk_requests`/`bulk_batches`/`avg_batch_size`/
  `max_batch_size`/`bulk_retries`/`retryable_failures_seen`/
  `terminal_failures`: observed directly from the real
  `opensearch_bulk`/`elasticsearch_bulk` module during the pass
  (`benchmarks/server_indexing/bulk_capture.py` wraps, but never
  changes, the real batching/retry functions). `bulk_requests` counts
  every HTTP bulk call including retries; `bulk_retries` is
  `bulk_requests` beyond one-per-initial-batch; `terminal_failures` is
  what's left after retries are exhausted (would raise `BulkIndexError`
  if nonzero).
- `entity_docs_before`/`entity_docs_after`/`file_docs_before`/
  `file_docs_after`: a direct `count` against the server's own content/
  files indices immediately before and after the pass.

`incremental` (an `IncrementalRunMetrics`) additionally reports:

- `files_modified_on_disk`/`files_added_on_disk`/`files_deleted_on_disk`/
  `files_renamed_on_disk`: what the corpus mutation
  (`corpus.apply_incremental_changes`, ~2%/1%/1%/1% by default) actually
  did on disk, independent of what the indexer reports.
- `entity_docs_deleted`/`file_docs_deleted`: the drop in server-side doc
  counts across the pass (0 if counts went up, as expected when adds
  outweigh deletes).
- `looks_like_full_rewrite`: `true` when `index_metrics.files_indexed` is
  far larger than the real on-disk change (more than 3x it, and over
  half the corpus) -- the tell that the incremental path degraded into a
  full reindex instead of touching only what changed. This is the
  headline correctness signal for the incremental variant.

### Recorded real-cluster results

`benchmarks/server_indexing/reports/opensearch_2100files_2026-09-26.json`
and `elasticsearch_2100files_2026-09-26.json` are **measured, real-cluster**
runs (2,100 files -- 2,000 generated + 100 Markdown docs, `--num-files
2000`) against single-node OpenSearch 2.15.0 / Elasticsearch 8.15.0
containers (`docker/docker-compose.*.yml`'s own images), captured in this
session's sandbox. Headline numbers:

| Metric (cold index)              | OpenSearch  | Elasticsearch |
| --------------------------------- | ----------: | -------------: |
| files indexed                     |       2,100 |          2,100 |
| wall time                         |     493.4s  |        509.2s  |
| files/sec                         |       4.26  |          4.12  |
| bulk actions / requests           | 51,791 / 2,305 | 50,192 / 2,305 |
| bulk retries / terminal failures  |         0/0 |            0/0 |
| entity docs published             |      12,700 |         12,700 |
| lexical p50 / p95                 | 11.65 / 16.83 ms | 14.97 / 24.76 ms |

| Metric (incremental: 32 modified, 16 added, 16 deleted, 16 renamed) | OpenSearch | Elasticsearch |
| --- | ---: | ---: |
| files scanned (full corpus)       |       2,100 |          2,100 |
| files actually reprocessed        |          48 |             48 |
| wall time                         |       8.34s |         8.60s |
| `looks_like_full_rewrite`         |       false |         false |

Both runs scanned the full 2,100-file corpus (a full directory walk is
still needed to detect what changed) but only **reprocessed and
published 48 files (2.3%)** -- exactly the 32 modified + 16 added
(deletes/renames don't add new content to reprocess), never anywhere
close to the full corpus. `linking_seconds` dominates the cold-index
wall time (~300s of ~500s) for this corpus on purpose: the generator's
every-module-calls-the-previous-module chain gives the cross-file
linker a genuinely non-trivial graph to resolve, which is exactly what
this benchmark is meant to stress that a purely offline/local-only
suite never would.

Re-run the suite yourself before trusting these numbers on different
hardware or corpus shapes -- they are a reproducible reference point,
not a performance guarantee.

### CI-safe harness test

`tests/unit/test_benchmarks_server_indexing.py` exercises the corpus
generator, metric/percentile math, bulk-capture instrumentation, and a
full end-to-end run of the harness (cold index + incremental) against a
small (~30 file) corpus and the fake in-memory OpenSearch client
(`tests/unit/_fake_opensearch.py`) used elsewhere in the unit suite. It
runs in CI on every PR. It is **not** a substitute for actually running
`python -m benchmarks.server_indexing` against a real cluster with
2,000+ files -- that is a manual/on-demand run, documented above.
