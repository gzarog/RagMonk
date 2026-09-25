# Indexing Optimization V2 / Completion — completion report

Plan id: `ragmonk-indexing-optimization-v2-completion`. Baseline:
`origin/main` @ `30007d00a24edfd45cfa1d57dddb1e55d41bf5a9` (P0-P7 of the
predecessor plan, `ragmonk-indexing-performance-v1`, already complete).
All six phases (P1-P6) landed on one branch, `feature/indexing-optimization-v2`,
one PR.

## Phases and what each actually did

- **P1 — Code derivation versioning.** `FileKind.CODE` now registers a
  `version_provider` (`code.processor.code_version_stamp`), the one real
  gap the P0-P7 baseline explicitly left open. Reuses the existing
  generic version-stamp comparison (`indexing/incremental.
  decide_reprocessing`), which the document pipeline already exercised
  since an earlier plan's Phase 12 — no new machinery needed, only the
  registration and the version constant.
- **P2 — Bounded document extraction concurrency.** `documents.pipeline`
  split into `prepare_document`/`publish_document`, mirroring the
  existing CODE precedent. New `indexing.document_extraction_workers`
  config (default `1`). `indexing/coordinator.py`'s bounded-parallel path
  generalized from CODE-only to any number of parallel-eligible kinds.
  `cap_native_thread_pools` caps torch's own thread pool when
  concurrency is engaged.
- **P3 — Persistent project-local embedding reuse.** New `embedding_cache`
  table (`KNOWLEDGE_DB_V15`), composite-keyed on exact text + model id +
  preprocessing version + embedding-text version, living in the same
  per-project `knowledge.db` every other project-scoped table already
  does (project isolation is therefore free). Never actively
  garbage-collected, matching `document_conversion_cache`'s own
  precedent.
- **P4 — Measured SQLite write-path completion.** New optional
  `storage.sqlite.count_statements` hook. Found and fixed a real N+1 in
  P3's own new hot path (`prepare_embeddings` calling
  `list_by_file`/`list_units_by_file` once per touched file); batched the
  delete/update side of `publish_embeddings`. Measured and **rejected**
  `executemany` for INSERTs (no real win) rather than keeping it.
- **P5 — Stage telemetry and trigger provenance.** Fixed a real bug: the
  daemon's actual trigger reason was tracked but discarded into a
  constant `"daemon"` string before dispatch. Added per-stage wall-clock
  timing (`IndexRunResult.timings`) and a DEBUG-level `stage_timings` log
  event, using the codebase's existing log-level mechanism rather than a
  new flag.
- **P6 — Benchmark matrix and rollout gate.** This phase: extended the
  benchmark harness, ran small/medium tiers plus four new V2-specific
  scenarios with a real (unmocked) embedder and Docling backends,
  verified migrations, reviewed the full diff, and produced this report.

## Measured gains (real numbers, this session's own runs)

- **P3's embedding-cache reuse**: a vector-rebuild-style re-embed over
  400 unchanged documents dropped from **2.445s to 0.081s** (a real,
  measured ~30x reduction for that specific operation), with 210 of the
  batch's unique texts served from the cache instead of the model.
- **P4's batched writes**: `publish_embeddings` on a synthetic 300-file
  batch went from 1,802 SQLite statements to 903 (delete/update side
  batched via `IN (...)`); `prepare_embeddings` went from 600 statements
  to 301 (fixing the P3-introduced N+1). Isolated delete+update
  microbenchmark: ~40-48% faster across three repeats.
- **P5's telemetry overhead**: measured at effectively zero — a 200-file
  synthetic pass averaged 0.3807s at the default `info` log level vs.
  0.3630s with `debug` telemetry fully enabled (within ordinary run-to-
  run noise, not a real difference).

## Defaults that changed

**None.** `indexing.document_extraction_workers` defaults to `1`
(serial), same as `indexing.code_extraction_workers`. This was
specifically measured in P6 (see `docs/indexing_benchmarks.md`'s V2
section) with real, unmocked document conversion: `workers=4` was
consistently *slower* than `workers=1` across two corpus sizes for this
project's rule-based document backends (plain text/HTML/CSV/Markdown).
This supersedes P2's own informal smoke test (which used artificially
slowed mock functions and showed an apparent speedup) with real evidence
pointing the other way for the formats this environment could measure.
No other config default changed anywhere in V2.

## Unchanged guarantees, reconfirmed

- Concurrency remains fully opt-in and serial by default (both
  `code_extraction_workers` and `document_extraction_workers`).
- No PRAGMA/durability changes anywhere in P4.
- No speculative indexes added — every hot query P4 touched already used
  an existing index, confirmed via `EXPLAIN QUERY PLAN`.
- Project isolation for the new embedding cache relies on the existing
  one-database-per-project layout, not a new access-control mechanism.

## Tests

Full suite as of the final P6 commit: **1030 passed, 2 skipped
(pre-existing, environment-only: POSIX permission-bit tests that don't
apply running as root in this container), 20 deselected** (existing,
unrelated `docling_pdf`/`embedding_model`/`benchmark_search`/etc. markers
excluded by this project's own default `pytest` config, unchanged by
this plan). No regressions at any phase boundary; each phase's commit
message in the PR history has its own focused pass/fail counts.

## Known limitations / deferred work (accumulated across all six phases)

- **P2/P6**: real PDF/OCR document conversion under concurrency was
  never benchmarked or exercised under load. Docling's layout/table-
  structure model weights and RapidOCR's model weights were not cached
  in this environment (only the embedding model's weights were), and
  downloading them was judged out of scope for this session's time
  budget. `cap_native_thread_pools`'s value is therefore unverified for
  the specific workload (ML-inference-heavy PDF/OCR) it was written to
  protect.
- **P5**: the `process` stage (prepare+publish combined) is not split
  into separate sub-timings — P2/P4's bounded-parallel path interleaves
  many files' prepare calls with one writer thread's publish calls, so
  there is no single clean boundary to time separately without either
  misleading numbers or non-trivial new per-file instrumentation.
- **P6**: 10k/200k file tiers, WSL-native vs. `/mnt/c`, and a real
  network-share source were not run — all explicitly optional per the
  plan, and not applicable or not time-feasible in this single Linux
  container session. `python -m benchmarks.indexing --tier large`/
  `--tier scale` reproduce the file-count tiers on demand.
- **P3**: no active garbage collection for `embedding_cache` — a
  deliberate, documented choice (mirrors `document_conversion_cache`'s
  own precedent), not an oversight, but worth re-evaluating if a
  project's cache table grows large enough to matter in practice.

## Optional scenarios not run — explicit list

- 10,000-file tier (`--tier large`)
- 200,000-file tier (`--tier scale`)
- WSL-native vs. `/mnt/c` filesystem comparison
- Network-share (SMB/NFS) source scenario
- Real PDF conversion benchmark (Docling ML pipeline)
- Real OCR benchmark (RapidOCR)
- Concurrent document extraction specifically under the PDF/OCR path
  (only the rule-based-backend path was measured)

No performance claim in this report, the PR description, or any commit
message in this plan is made for any of the above — they are listed as
not run, not implied complete.
