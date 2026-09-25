# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Indexing performance optimization plan (`ragmonk-indexing-performance-v1`),
Phase P6: measured SQLite and cross-link optimizations.

### Fixed

- `knowledge.linker.link_touched_files` no longer builds its document-side
  `namespace_by_file` map at all on a pass that touched no document files
  (the overwhelmingly common case: an ordinary code-only edit). Before
  this phase it was built unconditionally, which meant such a pass paid
  for one `files_repo.get()` round trip per code file in the *entire*
  project for a result nothing then read -- a real N+1 query pattern,
  proportional to total project size rather than what actually changed.
  New regression test (`test_code_only_pass_never_looks_up_other_code_
  files_in_the_project`) asserts this directly.

### Added

- `files_repo.get_many()`: fetches several files' records in one `SELECT
  ... WHERE id IN (...)` query. `knowledge.linker.link_touched_files` now
  uses it for both its touched-code-files lookup and (when document files
  were touched) the project-wide filename-candidate map, replacing what
  were previously N individual `files_repo.get()` calls in each case.
- `knowledge.linker`'s identifier matchers (`match_exact_identifier`,
  `match_qualified_identifier`, `match_alias`, `match_filename`) now
  compile each needle's whole-identifier regex pattern once per entity/
  filename candidate, reused across every document unit checked against
  it -- instead of rebuilding the same pattern string on every single
  (entity, unit) pair, the pre-P6 shape.

Both changes are pure internal refactors: `link_touched_files`'s inputs,
outputs, and stored `cross_links` rows are byte-for-byte unchanged (every
existing linker/cross-link test passes unmodified); only the query and
regex-compilation cost of computing them is reduced.

### Added

- `ragmonk vectors backfill [--source ID] [--json]`: computes vectors for
  already-indexed CODE/DOCUMENT files that have none yet for the current
  embedding model -- most commonly every file indexed while
  `search.semantic` was off, which a normal `ragmonk index` pass never
  revisits for this reason alone (a `NULL` `embedding_model_id` is
  deliberately never treated as "stale" by `indexing.incremental.
  decide_reprocessing`). Reuses entities/document sections already on
  disk; never reruns Tree-sitter/Docling extraction. New
  `files_repo.list_missing_embeddings()` finds the target files.
- `indexing.embedding_indexer.embed_touched_files` splits into
  `prepare_embeddings` (pure reads plus model inference, no writes) and
  `publish_embeddings` (the short transactional delete-old-generation/
  insert-new-generation write and version-stamp update).
  `indexing.runner.run_source_pass` now calls `prepare_embeddings`
  *before* opening its write transaction and only `publish_embeddings`
  inside it -- the (potentially slow) model inference step no longer
  holds `BEGIN IMMEDIATE` for the duration it used to. `embed_touched_files`
  itself is kept as a thin prepare+publish convenience wrapper for
  callers (tests, one-off scripts) that don't need the split.
- `prepare_embeddings` deduplicates identical embedding texts within one
  batch (e.g. two overloads sharing a signature, a document chunk
  duplicated across sections) before calling the model, fanning the one
  resulting vector back out to every subject that shared the text.
  Scoped to a single batch only -- never persisted or reused across
  files or projects -- so this can never leak a vector across a
  project/permission boundary.
- `retrieval.embedder.embed_texts` accepts an optional `batch_size`,
  wired from new `IndexingConfig.embedding_batch_size` (default `16`,
  unchanged) -- tunable per-machine without editing code. CPU-only
  benchmarking showed no reliable gain from raising it in this project's
  own test environment, so the default is left as-is; no speedup is
  claimed here that measurement didn't back up.
- `retrieval.ann.sync_index_for_files` now detects drift left over from
  an interrupted prior update (SQLite committed, the on-disk ANN index
  never got that pass's `save()`) by comparing the index's own size
  against `vector_items`' count after applying the *current* pass's own
  changes, and falls back to a full `rebuild_index` when they still
  disagree -- SQLite stays the always-authoritative source (blueprint
  section 49) an interrupted index can reconverge from, without needing
  a separate repair command.

---

Indexing performance optimization plan (`ragmonk-indexing-performance-v1`),
Phase P4: bounded parallel code extraction, one transactional publisher.

### Added

- `code.processor` splits into `prepare_code` (pure, read-only: Tree-
  sitter parse + entity/relationship extraction, no database access)
  and `publish_code` (the transactional delete-old-generation/insert-
  new-generation write, including cross-file symbol resolution against
  the live connection). `code_processor` itself is unchanged in
  behavior -- it just calls the two back-to-back, serially, exactly as
  before this phase.
- `indexing.coordinator.ProcessorRegistry.register()` accepts optional
  `prepare`/`publish` callables; a kind registering both becomes
  eligible for the coordinator's bounded parallel path.
  `IndexCoordinator._process_queue` runs a kind's `prepare` for up to
  `indexing.code_extraction_workers` files concurrently in a bounded
  thread pool -- **default `1` (fully serial, byte-for-byte the pre-P4
  path)** -- while `publish` always runs on the coordinator's single
  writer thread, one file at a time. `publish_code` re-verifies the
  file's content identity immediately before writing and raises
  `ContentChangedDuringProcessingError` (safely retried, same as
  `documents.pipeline`'s identical Phase P3 check) if it changed since
  being claimed -- a wider, real window under parallel extraction than
  the serial path ever had.
- Document extraction concurrency is explicitly **not** implemented in
  this phase -- see the PR's scope note for why.

### Added

- `IndexCoordinator.run()` accepts an optional `changed_paths` and
  `indexing.coordinator.ScanRequest` (`full`/`changed_paths`/`reason`)
  drives a *targeted* pass over exactly those paths instead of walking
  the whole source tree (findings F1/F2). Deletion stays precise (only
  a named, now-missing path with an existing record is ever deleted --
  never inferred from a walk), and rename identity is preserved when
  both halves of a move land in the same batch (the common case for a
  local editor/`git mv`, thanks to the watcher's own debouncing); a
  rename split across batches degrades to delete-then-recreate, a
  disclosed trade-off. `run_source_pass` gained a matching optional
  `scan_request` parameter; every existing caller (`ragmonk index`)
  keeps its original full-scan behavior unchanged.
- `Daemon` now accumulates each source's touched paths between passes
  and hands them to the coordinator as a targeted `ScanRequest` --
  a single local edit no longer triggers a full source scan. Startup
  and periodic reconciliation passes, and any batch larger than 200
  paths (a watcher-overflow proxy), still force a full scan, as does an
  offline/unreachable source (`IndexCoordinator._run_targeted` runs the
  same `check_root_accessible` check the full-scan path does before
  trusting anything).
- `NetworkSourceWatcher.on_trigger` now receives the exact changed-path
  set its poll tick already computed (finding F2) instead of firing
  with no arguments, so a detected network change reuses that diff for
  a targeted pass instead of the coordinator walking the network tree
  a second time from scratch.

---

Indexing performance optimization plan (`ragmonk-indexing-performance-v1`),
Phase P3: one verified file fingerprint, reused instead of independently
re-hashed at every downstream stage.

### Added

- `sources.fingerprint.FileIdentity`/`verified_hash`: `IndexCoordinator`
  now computes a file's content hash once during scan/classify and
  threads it (plus the exact size/mtime it was verified against)
  through `ProcessorContext.file_identity`. `documents.pipeline.
  document_processor` and `documents.docling_adapter.convert`/
  `_convert_pdf` reuse it via `verified_hash` -- a cheap `stat()`
  comparison, not a re-read -- instead of each independently hashing
  the whole file again (finding F4: the coordinator, the document
  pipeline and the PDF conversion cache previously did this
  independently, up to three full-file hashes for one PDF). A stat
  mismatch (the file changed since the coordinator's scan) still falls
  back to a full re-hash, exactly as if no identity had been supplied.
- `core.errors.ContentChangedDuringProcessingError`: `document_processor`
  now re-stats the file immediately before publishing and raises this
  if it no longer matches the identity it started with, instead of
  committing content derived from a file that changed mid-extraction.
  Handled by the existing per-file retry/backoff path like any other
  processor exception.

### Changed

- `documents.docling_adapter.convert`/`_convert_pdf` and
  `documents.pipeline.document_processor` gain an optional
  `content_hash` parameter/field; every caller that doesn't supply one
  (direct calls, most existing tests) keeps hashing the file itself,
  unchanged from before this phase.

---

Indexing performance optimization plan (`ragmonk-indexing-performance-v1`),
Phase P1: scan completeness and coalesced daemon scheduling.

### Added

- `sources.scanner.scan()` now accepts an optional `outcome:
  ScanOutcome` that records every directory `os.walk` could not list
  and every file whose `stat()` failed, instead of `os.walk`'s default
  silent no-op (`onerror=None`). `IndexCoordinator.run()` uses this to
  skip deletion reconciliation entirely whenever a scan is incomplete
  -- an unreadable subtree can no longer make its files look deleted
  (finding F6). New/changed files found in whatever *was* successfully
  scanned are still processed; only "missing means deleted" is
  suppressed. `IndexRunResult` gains `scan_incomplete` and
  `scan_errors`; `ragmonk index` prints a warning when this happens.
- `Daemon.enqueue_source()` now coalesces a burst of triggers for the
  same source into at most one queued pass plus at most one follow-up
  pass for whatever arrived while a pass was already running (findings
  F1/F3) -- previously every watcher event queued its own independent
  full pass, so N rapid edits to one source could queue N full
  scan+diff passes. `enqueue_source` also takes an optional `reason`
  for telemetry (`daemon_pass_completed`/`source_pass_queued` log
  events), and `daemon_pass_completed` now logs `scan_incomplete`.

### Fixed

- An inaccessible subtree during a scan (permission denied, a race
  with a delete, a remote path unmounting mid-walk) could previously
  cause every file under it to be deleted from the index on the next
  pass, since `os.walk`'s default behavior silently treats an
  unlistable directory as empty. It is now treated as "scan
  incomplete, retry" instead.

Admin UI plan: a built-in, **local-first administration web interface**,
started with `ragmonk ui`.

### Added

- **`ragmonk ui`** starts a FastAPI + Jinja2 + HTMX admin app on
  `127.0.0.1:8765` (options `--host`, `--port`, `--no-browser`) and opens
  the browser. HTMX is vendored as a static asset and the templates/assets
  ship inside the wheel, so the UI needs no Node.js and works fully
  offline.
- **Admin screens** for the dashboard, sources (add/enable/disable/remove +
  detail), indexing (start/rebuild/failed-files + live SSE progress),
  documents (filter/paginate + chunk inspection), search
  (lexical/semantic/hybrid), code knowledge (symbols/callers/callees/
  references/impact), AI providers (status + connectivity test), typed
  configuration editing, daemon control, health, backups (create/download/
  restore) + update status, logs, and system info.
- A reusable **application-service layer** (`ragmonk.service.*`:
  `status_service`, `source_service`, `index_service`, `document_service`,
  `search_service`, `config_service`, `health_service`, `backup_service`,
  `daemon_service`, `ai_service`, `knowledge_service`, `logs_service`) that
  the UI calls directly — it never shells out to CLI commands. `ragmonk
  status` now reads the same `status_service.collect_status` the dashboard
  and the `ragmonk_status` MCP tool use.
- **Security controls** (localhost-only default, double-submit CSRF token,
  Host-header validation against DNS rebinding, explicit confirmation on
  destructive actions, and credentials never rendered — only
  configured/not-configured). No authentication is included: the release is
  localhost-only and single-user.
- Documentation: `docs/ui.md` and a README section.

### Notes

- New runtime dependencies (`fastapi`, `uvicorn`, `jinja2`,
  `python-multipart`) are imported only when `ragmonk ui` runs; every other
  command path stays free of the web stack.

## [0.3.4]

Exact Tokenizer work, Phase 5 (final): **diagnostics and observability**.
This completes the exact-tokenizer series.

### Added

- `ragmonk doctor` gains a **Tokenizer** section reporting the pinned
  model id, revision and asset fingerprint, the model's maximum sequence
  length and the effective chunk ceiling, and -- by scanning every stored
  embedding payload against the model's real input limit -- a
  **truncation count that must remain zero** (the section fails if any
  payload would be silently truncated). This proves which tokenizer the
  active index was built with and that no payload exceeds the model limit.
- `ragmonk status --json` includes a `tokenizer` block (identity + chunk
  ceiling); `ragmonk search --explain` reports the tokenizer identity.
- Structured log events: `tokenizer_loaded` on tokenizer initialization,
  and a per-document `chunk_budget_diagnostics` event (context reductions,
  budget splits, oversized-table segmentation, max observed payload) that
  escalates to a `chunk_budget_invariant_violation` WARNING if a payload
  ever breaks the no-silent-truncation invariant.
- `tokenization.diagnostics` (`tokenizer_identity` / `scan_payloads`) and
  `documents_repo.iter_embedding_texts` back the above.

### Notes

- The chunker's `ChunkingDiagnostics` counters are now collected during
  real indexing (wired through `documents/pipeline.py`).

## [0.3.3]

Exact Tokenizer work, Phase 4: **index identity + recoverable rebuild**.

### Changed

- The document index-derivation identity (`document_version_stamp`) now
  embeds the exact tokenizer's identity -- pinned revision, asset
  fingerprint and maximum sequence length -- inside `chunker_version`.
  Because chunk boundaries depend on the tokenizer's bytes and the budget
  ceiling, any tokenizer change now reprocesses affected files through the
  existing graceful version-drift path (`decide_reprocessing`) on the next
  `index` run. No hard index rejection is imposed: an index built by the
  old estimator is simply detected as stale and rebuilt from its source
  files, file by file, like any other version bump.

### Added

- `ragmonk rebuild --fresh [--yes]`: a recoverable rebuild from source
  files. Each project's existing derived state (`knowledge.db` + vector
  index + metadata) is moved aside to `*.old` backups, the fresh index is
  built in its place, and the backups are discarded only once the rebuild
  completes without error. A rebuild that fails mid-way is rolled back to
  the previously active index, so a failed `rebuild --fresh` never leaves
  a source without a usable index. Registered source roots are verified
  reachable before anything is touched; `--fresh` prompts for confirmation
  unless `--yes` (or `--json`) is given. Source files are never modified.

## [0.3.2]

Exact Tokenizer work, Phase 3: **exact table budgeting**.

### Changed

- Table chunks are now budgeted against the exact contextual payload
  (caption + repeated header + row text + contextual breadcrumb + special
  tokens), split at row boundaries when the whole table overflows.
- The previous "oversized single row kept whole" exception -- which could
  still produce an embedding payload beyond the model limit -- is removed.
  A single data row that alone exceeds the budget is now segmented at
  **cell boundaries**, and a single cell that still overflows is
  **token-split** into fragments. Every child segment repeats the relevant
  column header(s) and a `Row N` provenance marker, so no table embedding
  payload exceeds the model limit and row/column/table provenance is
  preserved (`documents/table_renderer.segment_oversized_row`).
- `ChunkingDiagnostics` now records `oversized_table_rows` and
  `oversized_table_cells`; `max_payload_tokens` is measured over the
  emitted chunks only.

## [0.3.1]

Exact Tokenizer work, Phase 2: **exact, payload-aware paragraph budgeting**.

### Changed

- The document chunker (`documents/chunker.py`) now budgets every chunk
  against the **exact** embedding tokenizer and against the **full
  contextual payload** the embedder actually sees -- document title,
  section/heading breadcrumb, separators, body, and the model's special
  tokens -- not just the raw body. The guaranteed invariant for every
  embeddable chunk is `exact_tokens(contextual_text, with special tokens)
  <= max_tokens - safety_tokens`, so no normal embedding payload relies on
  the model silently truncating an over-long input.
- `documents/tokenization.py` no longer estimates: `count_tokens` and
  `split_by_token_budget` delegate to the exact tokenizer (the old
  `_PIECE_RE` / `_CHARS_PER_TOKEN` estimator is removed). Sentence-first
  splitting is retained, with an exact sub-word fallback for a single word
  that alone exceeds the budget.
- Long contextual headers are reduced deterministically when they would
  starve the body budget: keep the deepest/current heading, then the
  title, dropping oldest intermediate ancestors first, and only
  token-truncating an individually oversized heading/title as a last
  resort. The evidence body is never truncated to keep a breadcrumb.

### Configuration

- `documents.chunking.max_tokens` now defaults to `auto` (resolves to the
  embedding model's real maximum sequence length, 256) and represents the
  full model input ceiling. An explicit integer above the model limit is
  rejected (it would permit silent truncation).
- New `documents.chunking.safety_tokens` (default 4): a reserve kept below
  the model limit. `min_tokens`/`overlap_tokens` are now exact token
  counts.

### Notes

- The chunker exposes an optional `ChunkingDiagnostics` accumulator
  (chunks split by budget, contextual headers reduced, oversized table
  rows, max observed payload) that `doctor`/`status` will surface in a
  later phase.
- `chunker_version`/`embedding_text_version` advance to `2`, so existing
  derived indexes rebuild their chunks/vectors from source on the next
  index run.

## [0.3.0]

Start of the **Exact Tokenizer** work: RagMonk is moving from an
approximate ~4-characters-per-token estimator to the real tokenizer of
its embedding model (`sentence-transformers/all-MiniLM-L6-v2`), so every
embedding payload can be budgeted against the model's true input limit.
This ships across several releases (0.3.x); each is independently
testable, and the clean-break index rejection is deliberately staged for
a later release.

### Added

- **Pinned, bundled, offline exact tokenizer** (`ragmonk.tokenization`).
  A new `ModelTokenizer` service loads the real WordPiece tokenizer for
  the embedding model from tokenizer assets bundled inside the package at
  a pinned Hugging Face revision
  (`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`). It:
  - counts exact tokens (with or without the model's `[CLS]`/`[SEP]`
    special tokens) and splits text at exact sub-word boundaries;
  - loads lazily on first use and is cached once per process;
  - reads only bundled files -- it never contacts Hugging Face during
    indexing;
  - verifies every bundled asset against a SHA-256 manifest on load and
    fails loudly (no silent fallback to the old estimator) if an asset is
    missing or modified.
- `tokenizers` is now a direct, version-pinned dependency (it loads local
  files only -- no network, no `torch`, no `transformers`).
- One source of truth for the embedding-model / tokenizer identity
  (`ragmonk.tokenization.model_identity`); `retrieval/embedder.py` now
  imports `EMBEDDING_MODEL_ID` from it so the embedder and tokenizer can
  never drift onto two different models.
- `scripts/refresh_tokenizer_assets.py`, a maintainer tool to re-download
  and re-hash the bundled assets when the pinned revision changes.

### Notes

- Exact-tokenizer tests run in the **default** (offline) test suite --
  the bundled assets need no network or model-weight download.
- Lightweight CLI commands (`--help`, `version`, ...) still never import
  or initialize the tokenizer; the startup-import regression test now
  guards `tokenizers` too.

## [0.2.0]

### Changed

- **Renamed the entire application from Ragpilot/RAGpilot to RagMonk as a
  clean break.** This is a new application identity, not a compatibility
  upgrade.
  - Python distribution and package: `ragpilot` -> `ragmonk`
    (source tree moved from `src/ragpilot` to `src/ragmonk`).
  - CLI executable: `ragpilot` -> `ragmonk`.
  - Environment-variable prefix: `RAGPILOT_*` -> `RAGMONK_*`
    (e.g. `RAGPILOT_HOME` -> `RAGMONK_HOME`).
  - Runtime home: `~/.ragpilot` / `%LOCALAPPDATA%\RAGpilot` ->
    `~/.ragmonk` / `%LOCALAPPDATA%\RagMonk`.
  - Project configuration: `.ragpilot.yaml` -> `.ragmonk.yaml`;
    per-source ignore file `.ragpilotignore` -> `.ragmonkignore`.
  - MCP server/client key `ragpilot` -> `ragmonk`; all MCP tools
    renamed from `ragpilot_*` to `ragmonk_*`.
  - Installers, updater, and release automation now use the
    `gzarog/RagMonk` repository and produce `ragmonk-<version>-*`
    artifacts, a `ragmonk-release-artifacts` CI bundle, and a
    `ragmonk-dependency-manifest` SBOM with a `ragmonk_version` field.
  - `RagpilotConfig`/`RagpilotError` -> `RagMonkConfig`/`RagMonkError`;
    daemon thread names, logger names, and process labels use `ragmonk`.

### Removed

- **No backward compatibility.** There is intentionally no `ragpilot` CLI
  alias, no `ragpilot` import shim, no `RAGPILOT_*` environment-variable
  fallback, no `.ragpilot.yaml` fallback, and no migration from
  `~/.ragpilot` or `%LOCALAPPDATA%\RAGpilot`. Existing Ragpilot databases,
  indexes, embeddings, caches, backups, and install metadata are neither
  read nor migrated, and are never deleted automatically.

### Migration

RagMonk starts with an empty knowledge base. A former Ragpilot user must
reinstall RagMonk, re-register source folders, and rebuild the index:

```bash
ragmonk init
ragmonk source add /path/to/source
ragmonk index
```

Old Ragpilot data is left untouched; optional manual cleanup is documented
in `README.md`.

### Added

- `scripts/check_branding.py`, run in CI, fails the build if any old
  pre-rename product identifier survives outside the narrow historical /
  clean-break allowlist.
- `.github/workflows/main-release.yml` now recognizes a controlled
  `[release minor]` merge marker that bumps the minor version (advancing
  `v0.1.x` directly to `v0.2.0`) instead of the default patch bump, and
  refuses to overwrite an existing tag.

## [Unreleased]

### Added

- Phase 1: Production Foundation.
  - `ragpilot` CLI (Typer) with `init`, `source add|list|info|enable|disable|remove`,
    `index`, `status`, `doctor`, `health`, `config show|get|set`, and `version`.
  - Layered Pydantic v2 configuration (defaults < user config < project config
    < environment variables < CLI overrides).
  - Platform-aware runtime directory layout (`~/.ragpilot`, `%LOCALAPPDATA%\RAGpilot`),
    overridable via `RAGPILOT_HOME`.
  - Source registry with local/network detection, and file discovery with
    `.gitignore`/`.ragpilotignore` support, default excludes, and secret-filename
    exclusion.
  - Content hashing with mtime+size fast-skip for incremental indexing.
  - SQLite storage (WAL, foreign keys, busy timeout) with an explicit,
    idempotent migration system.
  - Durable job queue with exponential backoff and crash recovery
    (`PROCESSING` jobs are requeued on startup).
  - A pluggable file-kind processor registry (extension point for later
    phases' code/document parsers), shipping a default "raw" processor for
    Phase 1.
  - Structured JSON logging plus a Rich console renderer.
  - `PathGuard` to confine all filesystem access to registered source roots.
  - Stable CLI exit codes (0-8) mapped from a typed exception hierarchy.
  - Unit and integration test suite, plus a GitHub Actions CI workflow
    (Linux/macOS/Windows x Python 3.12).

- Phase 2: Code Intelligence.
  - Tree-sitter parsing via `tree-sitter` + `tree-sitter-language-pack`
    (prebuilt grammar wheels for every target platform, no source
    compilation) -- see "Language coverage" below for what is fully wired
    up vs. deferred.
  - `code/parser.py` (language detection + parsing), `code/extractor.py`
    (a single, language-agnostic engine driven entirely by per-language
    Tree-sitter `.scm` query files under `code/queries/`), `code/resolver.py`
    (best-effort call/inheritance/import resolution with a documented
    EXACT/HIGH/MEDIUM confidence ladder), and `code/framework_rules.py`
    (two narrow, HEURISTIC-only examples: Flask/FastAPI-style Python route
    decorators, ASP.NET-style C# `[Route]`/`[Http*]` attributes).
  - Normalized entity types (`Namespace/Class/Interface/Struct/Enum/
    Function/Method/Property/Field`) and relationship types (`CALLS/
    IMPLEMENTS/EXTENDS/IMPORTS/REFERENCES/CONTAINS/DEFINED_IN`) added to
    `core/models.py`, reusing Phase 1's model system.
  - A new, additive `entities`/`relationships`/`code_fts` (FTS5) schema
    migration (`storage/schema.py` `KNOWLEDGE_DB_V2`) and matching
    repositories (`entities_repo.py`, `relationships_repo.py`). Indexing a
    file's entities is atomic (delete previous generation, insert new,
    update FTS, all in one transaction), mirroring Phase 1's generational
    file-indexing pattern.
  - `CodeProcessor` registered against `FileKind.CODE` in the existing
    `ProcessorRegistry` (`indexing/coordinator.py`'s `ProcessorContext` grew
    a few coordinator-provided fields -- connection, file id, source root,
    next generation -- for this; its scan/enqueue/claim/retry loop is
    unchanged). A syntax error or a raised parser exception is isolated to
    that one file via the existing `index_errors`/retry machinery, exactly
    like Phase 1's "poisoned file" handling.
  - Read-only CLI commands `ragpilot symbol|callers|callees|references`,
    all `--json`-capable with the Phase 1 envelope, backed by a shared,
    depth- and result-capped graph traversal (`code/graph.py`) that Phase
    5's `impact` command is expected to reuse.
  - Parser golden tests per language, resolver confidence-ladder unit
    tests, framework-heuristic unit tests, a direct `code_fts` unit test,
    and an end-to-end CLI integration test (multi-file/multi-language
    project, broken-file isolation, a simulated Tree-sitter parse
    exception, and `--json` output) under `tests/fixtures/languages/`.

  **Language coverage**: Python, JavaScript, TypeScript/TSX, Go, Java,
  Rust and C# are all fully implemented end-to-end (extraction +
  resolution + golden tests) -- all seven of the blueprint's priority
  languages installed and parsed cleanly in this environment, so none were
  dropped. C#'s `base_list` does not distinguish a base class from
  implemented interfaces at the grammar level; every entry is
  conservatively tagged `EXTENDS` rather than guessing from naming
  conventions (documented in `code/queries/csharp.scm`). Any other
  extension `sources/detector.py` already classifies as code (e.g. `.rb`,
  `.sql`, `.sh`, `.php`, `.kt`) has no grammar wired up yet and is
  recorded indexed without extracted entities rather than failing.

- Phase 3: Docling Document Pipeline.
  - `documents/docling_adapter.py` (a thin wrapper around Docling's Python
    `DocumentConverter`), `documents/normalizer.py` (turns a
    `DoclingDocument` into a flat, index-addressed unit list with
    heading-path/page provenance on every unit), `documents/chunker.py`
    (groups that into size-bounded, still-provenance-carrying chunks),
    `documents/metadata.py` (document-level metadata -- whatever Docling
    exposes), and `documents/pipeline.py` (`document_processor`, wiring
    all four into storage).
  - **Supported formats**: PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT,
    EML. Any other extension `sources/detector.py` still classifies as
    `FileKind.DOCUMENT` (legacy `.doc`/`.ppt`/`.xls`, OpenDocument, `.rtf`,
    `.csv`, `.rst`) is recorded indexed without derived document content,
    mirroring Phase 2's "recognized extension, not wired up yet"
    fallback. OCR, images, audio/video, and VLM pipelines are explicitly
    out of scope for this phase (`documents.ocr` in `core/config.py` is
    read by nothing yet).
  - Normalized entity types `Document`, `Section`, `Paragraph`, `Table`
    added to `core/models.py`, plus `DocumentFormat`/`SectionKind` enums,
    reusing Phase 1/2's plain-Pydantic model conventions. Every row
    carries its heading path, page range, and a content hash where
    applicable -- documents don't need Phase 2's full
    entity/relationship/confidence machinery (no cross-document
    references exist yet), so hierarchy is stored directly via
    `parent_id` + a redundant `heading_path` rather than `CONTAINS`
    relationship rows.
  - A new, additive `documents`/`document_sections`/`document_fts` (FTS5)
    schema migration (`storage/schema.py` `KNOWLEDGE_DB_V3`) and a
    matching repository (`documents_repo.py`). Indexing a file's document
    content is atomic (delete previous generation, insert new, update
    FTS, all in one transaction), exactly like Phase 1/2's generational
    pattern.
  - `document_processor` registered against `FileKind.DOCUMENT` in the
    existing `ProcessorRegistry` (gated on `documents.enabled`). A
    corrupt/unreadable/unsupported-format document is caught and recorded
    via `index_errors` without crashing the coordinator or the rest of
    the run, exactly like Phase 1/2's file-level fault isolation. A PDF
    over `documents.max_pages` is marked `SKIPPED_LIMIT` (the same status
    Phase 1 uses for oversized files) -- checked via `pypdfium2`'s page
    count alone, before Docling's own conversion would ever run, so an
    oversized PDF never triggers a model download.
  - Fixed a latent bug `files_repo.delete` had carried since Phase 1: it
    never cascaded to Phase 2/3's derived-content tables
    (`entities`/`relationships`/`documents`/`document_sections`), so
    reconciling a deleted source file that already had derived content
    raised a foreign-key `IntegrityError` instead of deleting cleanly.
  - Read-only `ragpilot docs [--source ID] [--json]`, following Phase
    1/2's `--json` envelope and exit-code conventions.
  - **The `docling_pdf` test tradeoff**: Docling's PDF pipeline downloads
    layout/table-structure model weights from Hugging Face on first use.
    Every other supported format is converted by Docling's rule-based
    backends and is exercised by the default test suite with real
    fixtures (`tests/fixtures/documents/`, three of them --
    `document.docx`/`presentation.pptx`/`spreadsheet.xlsx` -- generated by
    a small dev-only script, `python-docx`/`python-pptx`/`openpyxl`
    declared as dev/test-only dependencies). PDF conversion tests are
    marked `@pytest.mark.docling_pdf` and excluded from the default
    `pytest -q` run (`pyproject.toml`'s `addopts`); run them explicitly
    with `pytest -m docling_pdf` (see CONTRIBUTING.md). CI runs them too,
    in a separate, non-blocking job.
  - **Deferred, out of scope for this phase**: OCR, images, audio/video,
    OpenDocument, EPUB, advanced VLM pipelines, cross-domain (code <->
    document) linking, `ragpilot search`/`explore`, and any daemon/watcher
    integration.

- Phase 4: Unified Knowledge Model.
  - `knowledge/linker.py`: a cross-domain linker connecting code entities to
    the documents that describe them (blueprint section 19). Implements the
    blueprint's priority list to the extent Phases 1-3 actually give it
    signal to work with: exact bare-identifier matching, qualified
    (fully-dotted) identifier matching (scored at least as strong as bare
    matching, per the blueprint), filename matching, a modest class-only
    alias of a qualified name, and route/method matching that reuses Phase
    2's existing `framework_rules.py`-tagged HTTP endpoint findings rather
    than building any new route/message-broker infrastructure. Explicit,
    user-defined mappings (`ragpilot link add`) are the highest-trust
    source and are never written, overridden, or contradicted by the
    automated linker (different `resolver` values, proven by a dedicated
    test). Semantic/embedding-based linking is explicitly out of scope
    here, per the blueprint ("semantic similarity may suggest links but
    must not silently create high-confidence facts") -- Phase 4 has no
    embedding infrastructure to do so anyway (that's Phase 9).
  - Confidence ladder (`knowledge/confidence.py`, re-exporting Phase 2's
    `Confidence` enum rather than duplicating it): explicit user mappings =
    EXACT; exact/qualified identifier matches = HIGH; filename/alias
    matches = MEDIUM; route-heuristic matches = HEURISTIC -- the same
    tiers Phase 2's `code/resolver.py` already established, documented in
    the same comment style.
  - `knowledge/entities.py`: a thin query facade unifying "look up an
    entity by id or canonical/qualified name" across code entities and
    documents, wrapping `entities_repo`/`documents_repo`/`files_repo`
    rather than adding a new persistence layer.
  - `knowledge/evidence.py`: normalizes a code relationship or a
    cross-domain link into the blueprint's evidence contract (section 57)
    -- `{source, path, location: {line_start, line_end, page, section},
    entity, relationship, confidence}` -- computed at read time from
    existing Phase 2/3 storage rather than materializing a separate
    `evidence` table (see "Storage design" below).
  - Storage: a new additive migration (`storage/schema.py`
    `KNOWLEDGE_DB_V4`, `cross_links` table) rather than reusing Phase 2's
    `relationships` table directly -- `relationships.target_entity_id` is
    a foreign key into `entities(id)` only, and a document is not an
    entity row, so a document-side link needs its own table
    (`entity_id`/`document_id`/optional `section_id`, `resolver`,
    `confidence`, `evidence`). No separate `evidence` table: evidence is
    derived at read time (`knowledge/evidence.py`) from
    `entities`/`relationships`/`documents`/`document_sections`/
    `cross_links`, which is simpler and has no known performance need yet
    to justify materializing it.
  - The cross-domain linking pass runs as part of `ragpilot index`
    (`cli/index.py`), after each source's per-file processor queue has
    fully drained -- a link needs both a code entity and a document to
    exist, so it cannot be computed per-file the way Phase 2/3's atomic
    generational writes are. It is incremental: only entities/documents
    belonging to files (re)indexed *this run* are matched, searched
    against the full existing project corpus in both directions (a new
    document against all existing code, and vice versa) -- a full-corpus
    recompute on every run does not scale. Stale links are cleaned up via
    the same generational-deletion pattern Phase 1-3 already use:
    `entities_repo.delete_by_file`/`documents_repo.delete_by_file` (and
    `files_repo.delete`) now cascade into the new `links_repo`, so a link
    pinned to a file's previous generation (including an explicit,
    user-defined one) is removed when that file is re-indexed with
    different content or deleted -- see `links_repo.py`'s docstrings for
    why that is unavoidable given Phase 2 entities have no identity stable
    across a content-changing regeneration.
  - Read/write CLI `ragpilot link add ENTITY DOCUMENT [--section ID]`,
    `ragpilot link remove ID`, `ragpilot link list [--entity NAME]
    [--document ID] [--json]`, following Phase 1-3's `--json` envelope and
    exit-code conventions. Deliberately minimal (per the blueprint, full
    `explore`/`impact` presentation is Phase 5's job): enough to inspect
    the link graph and manually correct it.
  - **Known limitations**: matching is exact/substring-based (with word-
    boundary checks) over `document_sections` text -- no fuzzy or semantic
    matching, and a common short identifier can produce a noisy bare-name
    match (no suppression heuristic beyond boundary-checking is applied).
    An explicit link pinned to a code entity only survives re-indexing
    while that entity's *file* is unchanged (classified `UNCHANGED` and
    never reprocessed) -- Phase 2 entity ids are not stable across a
    content-changing regeneration of their file, so a link pinned to one
    is cleaned up along with it, the same as an auto-discovered link
    would be. Cross-domain linking is scoped to one project (one
    source's `knowledge.db`) at a time; it does not link across two
    different registered sources.

- Phase 5: Retrieval.
  - `retrieval/planner.py`: a small, deterministic (no LLM) query
    classifier -- a bare identifier routes to identifier+FTS, "who calls
    X"/"callers of X" routes to symbol lookup + incoming `CALLS` graph
    traversal, "documents about X" routes to FTS (+ semantic once
    `search.semantic` is enabled), and "what breaks if X changes"/"impact
    of X" routes to symbol + callers + callees + tests + docs -- matching
    the blueprint's own four illustrative examples (section 25). A small,
    ordered set of regex rules, not an NLP query-understanding system, per
    the blueprint's "initially deterministic" scope.
  - `retrieval/lexical.py` and `ragpilot search QUERY [--limit N]
    [--json]`: merges exact/qualified-identifier matches, `code_fts`/
    `document_fts` hits, file-path substring matches, and document
    title/heading matches into one ranked list. Ranking order: exact
    symbol > qualified symbol > title/heading > FTS rank > path relevance,
    with entity kind, file recency (mtime), and source id as deterministic
    tie-breakers within a tier (there is no stored per-entity-kind
    usage-frequency or source-priority signal in Phases 1-4 to rank
    *across* tiers on, so those three fold in as tie-breakers instead of
    being invented as new top-level signals).
  - `retrieval/graph.py`: composes Phase 2's existing `code/graph.py` BFS
    (`traverse`/`traverse_symbol`/`find_symbol_matches`) rather than
    reimplementing graph walking. `cli/references.py`'s incoming+outgoing
    CALLS/IMPORTS/REFERENCES aggregation was extracted here (behavior-
    preserving refactor, same output) so `impact`/`explore` reuse it too.
    Also adds edge *resolution* (`resolved_incoming`/`resolved_outgoing`,
    pairing each traversal edge with its neighboring `Entity`/file) and
    `is_test_file`/`find_tests_referencing`: a small, documented,
    naming-convention filter (`test_*.py`, `*_test.py`/`.go`,
    `*Test(s).cs`/`.java`, `*.test.ts(x)`/`*.spec.ts(x)`, etc.) over
    Phase 2's already-stored CALLS/IMPORTS/REFERENCES edges -- not a new
    test-framework-detection subsystem, and its result's provenance is a
    filename guess, independent of each edge's own `Confidence`.
  - `cli/impact.py` and `ragpilot impact SYMBOL [--max-depth N] [--limit
    N] [--json]`: defining location(s), callers/callees (Phase 2's CALLS
    graph -- the blueprint's "Produced by"/"Consumed by" vocabulary maps
    onto `PRODUCES`/`CONSUMES` relationship types Phases 1-4 never
    populate, see `core/models.py`), the tests heuristic above,
    cross-domain document links (Phase 4's `links_repo`, shaped through
    `knowledge/evidence.py`), a combined code/document confidence summary,
    and a "blast radius" bucket. Blast radius is a deliberately simple,
    documented heuristic -- `LOW` (score 0-2), `MEDIUM` (3-7), `HIGH`
    (8+) -- over one count (distinct callers + distinct linked documents
    touched), not a calibrated model.
  - `retrieval/context_builder.py`: the blueprint's evidence-package
    assembly (section 27) -- dedupes same-file/location evidence (keeping
    the higher-confidence one), prioritizes exact evidence over heuristic,
    and enforces a new `context:` config budget (`max_chars`, `max_files`,
    `max_graph_nodes`, defaults 30000/20/100) with explicit, reported
    truncation rather than a silent drop. Graph relationships are carried
    as `A -[REL]-> B` paths, not just bare endpoints. A pure function with
    no database access of its own, built on `knowledge/evidence.py`'s
    existing evidence shape.
  - `cli/explore.py` and `ragpilot explore "QUERY" [--json]`: the primary
    retrieval command (blueprint section 25) -- runs the planner's chosen
    strategies and returns Summary/Relevant symbols/Relevant paths/Call
    flows/Dependencies/Documents/Tests/Requirements/Incidents/Evidence.
    Summary is a short, deterministically-templated string, never an LLM
    summary (no LLM is used anywhere in this phase). "Requirements" and
    "Incidents" are document *categories* the blueprint lists as example
    entity types (section 17) that Phases 1-4 never classify a document
    into, so those two sections always come back empty rather than this
    phase inventing a document classifier to fill them.
  - `retrieval/semantic.py`: the seam for Phase 9's real semantic/vector
    search -- returns a clearly-marked skipped result while
    `search.semantic` is `false` (the default) and raises a specific,
    typed `SemanticSearchNotImplementedError` if ever invoked while
    enabled, so `planner.py` (and later Phase 6's MCP tools) have one
    stable place to plug it in without this phase faking a result.
  - Unit tests for the planner's classification rules (each blueprint
    example plus supporting cases), lexical ranking (fixtures isolating
    exact/qualified/FTS/path/title signals), the context builder's
    dedup and all three budget knobs independently, the tests-heuristic
    filename patterns, and blast-radius bucket boundaries; an end-to-end
    integration test indexing a mixed code+document project with a
    cross-domain link and exercising `search`/`impact`/`explore`
    (including `--json`) through the real CLI.

- Phase 6: MCP Server.
  - `mcp/server.py` and `mcp/tools.py`: a stdio MCP server (blueprint:
    "MCP over stdio as the default agent transport, no external listening
    port") built on the official `mcp` SDK's high-level
    `mcp.server.fastmcp.FastMCP` decorator/registry API (pinned `mcp>=1.2,
    <2` -- the SDK's 2.x line renames `FastMCP` to `MCPServer` and
    reshuffles its API; 1.x's `FastMCP` is the well-established, directly
    testable one), exposing 8 tools: `ragpilot_explore` (primary),
    `ragpilot_search`, `ragpilot_symbol`, `ragpilot_callers`,
    `ragpilot_callees`, `ragpilot_impact`, `ragpilot_documents`,
    `ragpilot_status`. Each tool is a thin adapter over the exact same
    functions the equivalent CLI command calls (`code/graph.py`,
    `retrieval/lexical.py`, `retrieval/planner.py`, and newly-extracted
    `_run` helpers in `cli/docs.py`/`cli/status.py`/`cli/impact.py`
    mirroring `cli/explore.py`'s existing split) -- no retrieval logic is
    reimplemented, and every tool call bootstraps the same `AppContext`
    (same `RAGPILOT_HOME`/config resolution) a CLI invocation would, so an
    agent sees exactly what `ragpilot explore`/`search`/... would show.
  - `mcp/schemas.py`: one explicit, versioned Pydantic output model per
    tool (`schema_version`/`ok`/`error` on every response, mirroring the
    CLI's own `--json` envelope convention) instead of an ad hoc dict, so
    FastMCP derives deterministic JSON Schema straight from the typed
    signatures/models.
  - Bounded responses: `ragpilot_explore` routes through Phase 5's
    `retrieval/context_builder.py` budget (`context.max_chars`/
    `max_files`/`max_graph_nodes`), overridable per call via optional
    tool arguments.
  - Clear errors: a new `mcp/tools.py`-internal mapping turns any
    `RagpilotError` into a typed `{"type": "UsageError", "message": ...}`
    -shaped result (by exception class name) rather than a raw traceback
    or protocol-level failure -- "no such source", an empty query, an
    unresolvable symbol, etc. all come back as a normal, `ok=false` tool
    result a client can branch on.
  - Timeout enforcement: a new `mcp.request_timeout_seconds` config field
    (default 30s) bounds every tool call via `asyncio.wait_for` around an
    `asyncio.to_thread`-run synchronous call. Documented as an honest
    wall-clock "stop waiting", not true cancellation: Python cannot force-
    kill a running thread, so a timed-out call's thread runs to
    completion and cleans itself up in the background rather than
    blocking the client any further.
  - stdout/stderr discipline: every MCP-tool-call `AppContext` is
    bootstrapped with `log_console_format="json"` so WARNING+ console
    logs go to stderr (plain `logging.StreamHandler`) instead of the
    CLI's interactive, stdout-default `RichHandler` -- stdout is the MCP
    stdio transport's JSON-RPC framing channel, and a stray log line on
    it would corrupt the protocol stream.
  - `ragpilot serve --mcp`: starts the blocking stdio server loop after
    checking `mcp.enabled` (fails fast with `ConfigError`/exit code 3 if
    disabled, never silently starting anyway) -- `--mcp` is required
    since it is the only transport this phase implements (the optional
    REST API is Phase 8). Server *construction* (`mcp/server.py`'s
    `build_server`) is pure and unit-tested directly; the blocking stdio
    loop itself is not exercised in the test suite.
  - `ragpilot install-agent [--write PATH]`: prints the standard
    `{"mcpServers": {"ragpilot": {"command": "ragpilot", "args": ["serve",
    "--mcp"]}}}` JSON snippet most MCP clients expect, and only writes it
    to disk when given an explicit `--write PATH`. Deliberately does not
    discover or edit any real client config file (e.g. `~/.claude.json`)
    on its own -- printing/writing only to a path the user names is the
    safe version of blueprint section 74's "connect AI agent" step.
  - Unit tests calling all 8 tool functions directly (a FastMCP-decorated
    tool is still just its underlying coroutine) against a small indexed
    fixture project, plus a context-budget test, a bad-input/unknown-
    symbol structured-error test, a timeout test (a monkeypatched slow
    retrieval call proves the configured timeout fires rather than
    hanging), `mcp/server.py` construction tests, and CLI tests for
    `serve --mcp`'s `mcp.enabled=false` fast-fail path and
    `install-agent`'s print/`--write` output.

- Phase 7: Incremental Runtime.
  - **Offline-vs-deleted safety fix** (applies to `ragpilot index` too,
    not only the new daemon): `sources/scanner.py`'s `scan()` is built on
    `os.walk()`, which silently yields nothing for a root it cannot list
    (its default `onerror` is a no-op) -- indistinguishable, to a caller
    only watching `scan()`'s output, from a root that is genuinely empty.
    A new `check_root_accessible()` performs the one real syscall
    `os.walk` itself skips (listing the root) before any run is allowed
    to treat `scan()`'s output as trustworthy for deletion reconciliation.
    `indexing/coordinator.py`'s `IndexCoordinator.run()` checks it first:
    if the root is unreachable, the whole scan/diff/delete pass is
    skipped entirely for that run (every existing file/entity/document
    stays exactly as it is, still queryable) and the run reports
    `source_offline`/`offline_reason` instead. A new `Source.status`
    (`core.models.SourceStatus`: `active`/`offline`, additive
    `sources.db` migration v2) records this, flipping back to `active`
    (and reconciling for real) the moment the root is reachable again.
    `ragpilot source list|info` surface the status; `ragpilot doctor`
    reports an offline source as a WARN (not a hard FAIL of the whole
    health check) using the same `check_root_accessible()` check, live.
    Proven by a dedicated integration test that simulates a source root
    disappearing mid-project and asserts every existing entity survives
    an `index` run, the source flips OFFLINE then back to ACTIVE, and a
    real deletion/addition made while it was "away" is only reconciled
    once the root is restored.
  - `indexing/runner.py`: the per-source scan+process+link+status-
    transition pass factored out of `cli/index.py` into
    `run_source_pass()`/`build_processor_registry()` -- the exact unit of
    work both `ragpilot index` and the new daemon trigger, so the daemon
    is an orchestration layer over the existing pipeline, not a second
    implementation of it.
  - `watcher/local.py`: a `watchdog`-based (new dependency: native
    inotify/FSEvents/ReadDirectoryChangesW, no heavy transitive deps)
    observer per local source root, debounced by `watcher/debounce.py`'s
    per-key `Debouncer` -- reuses Phase 1's `indexing.debounce_ms` config
    field rather than adding a parallel setting, since there is exactly
    one "how long to wait for a burst of writes to settle" knob whether
    the burst came from a filesystem event or a network poll tick.
  - `watcher/network.py`: a polling loop per network/UNC source root
    (mtime+size fingerprint, reusing `scanner.scan()`'s traversal/ignore
    rules and offline check) since native filesystem events don't
    reliably cross a network mount. Interval is a new
    `indexing.network_poll_seconds` config field (default 30s).
  - `service/daemon.py`: the daemon loop (`Daemon`). Every enabled source
    gets a watcher (local: `watchdog` + debounce; network: polling); every
    trigger -- a debounced local event, a poll tick finding a change, or
    the periodic reconciliation timer -- funnels through one worker
    thread into `run_source_pass()`, serialized against Phase 1's exact
    `RunLock` (acquired/released fresh per pass, not held for the
    daemon's lifetime) so a concurrent manual `ragpilot index` and the
    daemon never interleave writes to the same database. A new
    `indexing.reconciliation_interval_seconds` config field (default
    900s/15min) drives a full rescan+diff pass per source as a safety net
    independent of watcher events, proven by a test where a change made
    without going through the watcher's event path (a debounce set
    effectively infinite) is still caught on the next reconciliation
    tick. Graceful shutdown (SIGINT/SIGTERM): stops accepting new
    triggers immediately, but the worker thread is joined rather than
    killed, so a pass already in flight always finishes its own
    (already-atomic, per-file) transactions before the `RunLock` is
    released -- proven by a test that sends a stop signal mid-pass and
    asserts every file still ends up fully `INDEXED`, the job queue ends
    empty, and the lock is immediately re-acquirable afterward.
  - `service/pid.py` / `service/health.py`: PID-file bookkeeping (built
    on Phase 1's `RunLock`, not a second locking mechanism -- the lock
    already prevents two writers; this only lets a *different* CLI
    invocation find and signal the running one) and a heartbeat/health
    JSON snapshot (uptime, last reconciliation time, per-source watcher
    online/offline state) the daemon writes after every pass, so `ragpilot
    daemon status` can report on a running daemon without talking to its
    process directly.
  - `ragpilot watch`: foreground, blocking daemon loop (mirrors `serve
    --mcp`'s pattern -- a thin, deliberately-untested blocking
    entrypoint over fully unit-tested logic). `ragpilot daemon
    start|stop|restart|status [--json]`: spawns/signals/reports on a
    detached background process running the same loop (POSIX:
    `start_new_session=True`; Windows: `CREATE_NEW_PROCESS_GROUP |
    DETACHED_PROCESS`).
  - `check_same_thread=False` added to `storage/sqlite.py`'s `connect()`:
    the daemon's worker/reconciliation threads legitimately reuse
    connections opened on the main thread, with access serialized through
    the daemon's own lock rather than sqlite3's default same-thread
    check (which only knows which thread *opened* a connection, not
    whether access is otherwise serialized).
  - New `daemon_subprocess` pytest marker (excluded from the default run,
    same tradeoff Phase 3 made for `docling_pdf`, and run in the same
    style of separate non-blocking CI job): spawning and signaling a real
    detached OS process is slower and more platform-fragile than
    everything else in the suite. The daemon loop itself -- watcher
    wiring, debounce, reconciliation, offline/online transitions,
    graceful shutdown, PID/health bookkeeping -- is still fully covered
    by the default suite via direct unit/integration tests that never
    spawn a process.

- Phase 8: Operations.
  - **`ragpilot backup [PATH] [--json]`**: an online, consistent snapshot
    of `sources.db` and every registered source's `knowledge.db`, packaged
    into one `.tar.gz` archive alongside the user config and a
    `manifest.json` (RAGpilot version, per-database schema version,
    timestamp, included sources/projects). Uses SQLite's own
    `sqlite3.Connection.backup()` API rather than a raw file copy -- a
    plain copy of a WAL-mode database's main file can miss committed
    writes still sitting only in the `-wal` file, or capture a torn
    snapshot; the online backup API reads through SQLite's own
    consistent-snapshot machinery instead, which is also what makes it
    safe to run without stopping the daemon (proven by a test that backs
    up while a write sits uncheckpointed in the WAL and asserts a naive
    `shutil.copy2` of the same moment would have missed it). Deliberately
    does **not** include the original source files -- those remain the
    user's own data and the blueprint's source of truth; only
    derived/registry state is backed up. Coordinates with a live
    daemon/`ragpilot index` by acquiring the same `index` `RunLock` they
    use per pass, rather than refusing outright.
  - **`ragpilot restore ARCHIVE [--json]`**: verifies the archive
    (readable tar.gz, has a manifest, every included database's `PRAGMA
    integrity_check` passes), verifies the manifest's declared archive
    format version and each database's schema version aren't newer than
    this RAGpilot understands (refuses rather than silently risking
    corruption), stops a running daemon first (reusing Phase 7's
    `service/pid.py` detection/signaling, waiting for it to actually
    exit), and only then **atomically swaps** the verified state into
    place: every live path is first moved aside (not deleted) via
    same-filesystem `os.rename` into a holding directory, the staged
    (already-verified) state is renamed into place, and only on full
    success is the holding directory discarded -- any failure during the
    swap puts the moved-aside originals back, and every check before the
    swap runs against the temp-extracted archive copy only, so a failure
    at or before that point never touches the live runtime directory at
    all. Restarts the daemon afterward only if it had been running.
    Proven by an integration test that indexes a project, backs it up,
    deletes the live `sources.db`/`projects/` entirely, restores, and
    asserts `status`/`symbol` report identical data to before; and a
    second test that a deliberately corrupted archive is refused with the
    pre-restore state byte-for-byte (via `status --json`) unchanged
    afterward.
  - **`ragpilot rebuild [--source ID] [--json]`**: the blueprint's
    "source files = truth, RAGpilot DB = rebuildable derived state"
    principle made runnable -- deletes one source's (or every enabled
    source's) `knowledge.db` outright (source files on disk are never
    touched) and re-indexes it from scratch through the exact same
    `indexing/runner.run_source_pass` Phase 7's daemon and `ragpilot
    index` already use, rather than a second indexing implementation. A
    new `AppContext.close_project_conn()` drops the cached connection
    before the file is deleted, so a stale open handle can't keep the old
    file alive underneath the delete. Proven by a test that indexes a
    project, captures `symbol`/`callers`/`docs` output, rebuilds, and
    asserts the same entities/relationships/documents come back
    (comparing on stable, content-derived fields, since ids are freshly
    generated on every index run).
  - **`ragpilot upgrade [--json]`**: explicit, backup-first orchestration
    of the existing migration system (`storage/migrations.py`) across
    `sources.db` and every project's `knowledge.db` -- it does not
    reimplement migration application, which was already idempotent and
    automatic on every `AppContext.bootstrap()`/`project_conn()` call
    since Phase 1. This command's value: it checks what's pending
    *before* anything is touched (raw, read-only connections -- opening a
    normal `AppContext` first would apply `sources.db`'s migrations as a
    side effect and defeat the "before" half of a before/after report),
    takes an automatic backup (reusing `ragpilot backup`'s own logic, not
    a second implementation) only when a migration is actually pending,
    then lets the normal bootstrap path apply it, and finally reuses
    `ragpilot doctor`'s exact health-check logic (`cli/doctor.py`'s
    `run_checks`/`overall_status`, the latter promoted from a private
    helper for this reuse) to report whether the result looks healthy.
    **No automatic rollback**: reversing an already-applied SQLite schema
    migration safely (including undoing an additive `ALTER TABLE ADD
    COLUMN`, which SQLite itself cannot drop on every version this
    project supports) is meaningfully riskier and more complex than this
    phase's priority ordering warrants; if `healthy_after` comes back
    false, the documented recovery path is `ragpilot restore
    <backup_archive>` using the backup this same command just took. A
    no-pending-migrations run is a proven no-op (no backup taken, no
    database file touched); a pending-migration run is proven to back up
    first (its own copy of the database still shows the pre-upgrade
    schema version) and apply the migration successfully afterward, using
    the same "monkeypatch `storage.migrations.MIGRATIONS` down to an
    earlier subset, apply, restore the real mapping" technique Phase 1's
    migration tests established for constructing a stale database.
  - **Metrics**: `ragpilot status --json` now also reports a `metrics`
    block per source and in `totals` -- `symbols_created`/
    `relationships_created` (Phase 2 entity/relationship counts),
    `documents_processed` (Phase 3 document count), `database_size_bytes`
    (actual file size on disk), and `files_discovered`/`files_indexed`/
    `files_failed`/`index_queue_depth` (re-derived from data Phase 1
    already tracked, under the blueprint's own metric names). All real,
    cheaply-obtainable numbers computed directly from each project's
    `knowledge.db` -- nothing here is fabricated. **Deliberately not
    included**: a query-latency metric (e.g. average `explore`/`search`
    duration) -- no code path in this codebase times a query today, and
    adding a fake or hastily-wired one only to populate a metrics field
    would violate the "real, honestly-obtainable data" bar this phase set
    for itself; a real timing mechanism is left for whichever future
    phase actually needs to act on query latency. No Prometheus/metrics-
    server endpoint -- explicitly optional per the blueprint, not needed
    to satisfy this phase's requirements.
  - **Packaging/release integrity** (scoped down, same tradeoff Phase 3
    made for Docling's PDF pipeline and Phase 7 made for daemon-subprocess
    testing): `scripts/generate_release_artifacts.py` builds the sdist/
    wheel via the project's existing `hatchling` backend (through the
    standard `build` package), computes a `SHA256SUMS` checksum file, and
    writes a plain, hand-rolled dependency manifest (`sbom.json`: name/
    version/license per runtime dependency, read via `importlib.metadata`)
    -- explicitly *not* a CycloneDX/SPDX-standard SBOM, since pulling in a
    dedicated SBOM tool for this project's nine runtime dependencies would
    be disproportionate tooling weight; the manifest says so in its own
    `format_note` field. A new `.github/workflows/release.yml`, triggered
    only on version tags (`v*.*.*`, never on a PR or a push to `main`, so
    it cannot affect `ci.yml`'s gating), runs this script and attaches its
    output to the tag's GitHub release. **Explicitly out of scope and not
    attempted**: code signing (no signing certificate or secret exists in
    this repository/environment -- a stubbed or fake signature would be
    actively misleading, not a safe placeholder) and a standalone
    single-executable bundle for 6 OS/arch targets (PyInstaller or
    similar) -- a substantial, separate undertaking with its own risk
    surface, judged out of reach for this PR. Every artifact this
    workflow produces is clearly labeled **unsigned**, both in the
    release body text and in `generate_release_artifacts.py`'s own
    docstring/console output. Validated locally (per this phase's testing
    requirements: a GitHub Actions tag trigger cannot be run locally) by
    actually running the script end-to-end against a real build and
    verifying the resulting `SHA256SUMS` with `sha256sum -c`.
  - New `src/ragpilot/ops/` package (`backup.py`/`restore.py`/
    `rebuild.py`/`upgrade.py`): the orchestration logic behind all four
    commands above, kept separate from `cli/` (which stays a thin Typer
    wrapper per command) so it is unit-testable without going through
    Typer -- mirroring how `indexing/runner.py` already sits underneath
    both `cli/index.py` and the Phase 7 daemon.

- Phase 9: Optional Intelligence (the blueprint's final phase). Everything
  here is opt-in and never required for `explore`/`search`/`impact`/
  `symbol`/`callers`/`callees`/`docs` to keep working exactly as before --
  `search.semantic` still defaults `false` and no AI provider is
  configured by default, so the full Phase 1-8 test suite passes
  unmodified with this phase's code present but switched off.
  - **Local embeddings** (`retrieval/embedder.py`): real, local (offline
    at inference once weights are cached) sentence embeddings via
    `sentence-transformers/all-MiniLM-L6-v2`, called directly through
    `transformers`' `AutoTokenizer`/`AutoModel` (hand-rolled mean-pooling
    + L2 normalization) rather than adding the `sentence-transformers`
    package on top. Both `torch` and `transformers` are already required,
    non-optional dependencies via Docling's own PDF pipeline; for one
    fixed model, everything `sentence-transformers` adds is ~15 lines
    here, whereas the package itself would pull in its own dependency
    tree (scikit-learn/scipy/Pillow/tqdm) for no benefit this project
    needs -- the same "biggest transitive win, smallest add-on" call
    Phase 3 made for Docling's own OCR/VLM extras. `EMBEDDING_MODEL_ID`/
    `EMBEDDING_DIM` are stamped onto every stored row (see below): if the
    configured model ever changes, similarity search filters to the
    current `model_id` rather than ever mixing vectors from two models in
    one comparison -- a since-changed model's rows are simply excluded,
    not auto-re-embedded project-wide (see the indexing note below for
    why). A model that fails to load (no network on first use, no cached
    weights, `torch`/`transformers` missing) raises a specific
    `EmbeddingModelUnavailableError` that every caller catches to degrade
    to "semantic search unavailable" rather than crashing.
  - **Vector storage** (`storage/repositories/embeddings_repo.py` +
    `storage/schema.py`'s additive `KNOWLEDGE_DB_V5`/migration 5): a new
    `embeddings` table (subject type/id, file id, model id, dimension, a
    packed little-endian float32 BLOB vector), stored separately from and
    additive to `entities`/`documents`/`document_sections` -- losing or
    clearing this table can never touch those authoritative rows, only
    semantic search's own availability. Chose plain BLOB storage plus
    brute-force cosine similarity in application code
    (`retrieval/vectorstore.py`, pure Python, no new dependency) over a
    loadable SQLite extension like sqlite-vec: Python's `sqlite3`
    module's `enable_load_extension` support is not guaranteed
    available/enabled on every platform/Python build, exactly the class
    of cross-platform SQLite risk this project has already been burned by
    in Phases 5-7 (see this file's own Windows/macOS-specific fixes
    below), and there was no live multi-OS test available here to verify
    it either way. A linear scan is entirely adequate at the scale a
    local per-project knowledge base actually holds -- a legitimate,
    blueprint-sanctioned "equivalent embedded index", not a corner cut.
  - **Wired into retrieval** (`retrieval/semantic.py`, now a real
    implementation of Phase 5's pre-wired seam): embeds the query with
    the same model used to embed indexed content, scores it against every
    current-model embedding via `vectorstore.top_k`, and returns
    `SemanticHit`s in a shape deliberately close to `retrieval/
    lexical.py`'s `SearchResult` (same `kind`/`id`/`title`/`path`/
    `source_id`/`snippet`/`location`, plus a `score` the lexical shape has
    no equivalent of) so they merge cleanly into the same evidence
    pipeline without becoming a parallel, incompatible result type.
    `retrieval/planner.py`'s already-existing `Strategy.SEMANTIC` hook
    (added in Phase 5, never previously acted on) is now actually
    consumed by `cli/explore.py`'s `_run`, and `cli/search.py` surfaces
    semantic hits as their own distinct section/JSON key, never merged
    into the lexical ranked list. Per the blueprint (sections 19/57):
    semantic hits are folded into evidence at `Confidence.HEURISTIC` --
    the ladder's existing lowest tier, never EXACT/HIGH/MEDIUM -- so
    similarity can only ever *suggest*, never silently mint or upgrade a
    high-confidence fact. `ragpilot index`'s per-file processing computes
    embeddings for touched files only, gated entirely behind
    `search.semantic` (`indexing/embedding_indexer.py`, wired into
    `indexing/runner.py` right after the existing cross-domain-linking
    pass) -- enabling that one config value is what turns on both
    computing and using embeddings, and disabling it means `torch`/
    `transformers` are never even imported. Mirrors `knowledge/
    linker.py`'s own "touched files only" scoping and its documented
    tradeoff: freshly enabling `search.semantic` on an already-indexed,
    otherwise-unchanged project computes no embeddings until something
    touches those files again -- `ragpilot rebuild` (every file becomes
    "new") is the documented way to force a full backfill. Every
    `explore`/`search` query still works, unchanged, with
    `search.semantic` left at its default `false`, and degrades
    gracefully (never errors) if it is enabled but nothing has been
    embedded yet, embeddings were cleared, or the model can't load.
  - **Reranking**: no separate `retrieval/reranker.py` module. Ranking
    and merging already live in `retrieval/lexical.py`'s existing
    `RankTier` system (a deliberate Phase 5 decision, documented in that
    module's own docstring, to keep exactly one signal-to-tier mapping
    and one merge step); semantic results now participate in the overall
    retrieval picture through `cli/explore.py`'s evidence assembly and
    `cli/search.py`'s own distinct section rather than a second merge
    step reranking lexical and semantic hits together. Scoped down
    deliberately (Priority 3) rather than building a half-finished
    separate module.
  - **LLM provider abstraction** (`src/ragpilot/ai/`): `base.py` defines
    a small, provider-agnostic interface (`AiProvider.answer(AiRequest)
    -> AiAnswer`) plus one shared evidence-prompt builder every provider
    reuses -- a consumer of the evidence Phase 5's context builder
    already assembles, never a second knowledge-model owner, per the
    blueprint's own framing. Four real providers: `openai.py`/
    `anthropic.py` (the official `openai`/`anthropic` PyPI packages,
    pinned to each SDK's well-documented, `httpx`-based major --
    `openai>=1.0,<2`/`anthropic>=0.25,<1` -- rather than a much newer
    major this index also serves that vendors its own forked HTTP client
    internally, which would have undermined the mock-`httpx.Client`
    testing approach below), `ollama.py` (local HTTP API via `httpx`
    directly against `http://localhost:11434` by default, no dedicated
    SDK package exists for it), and `openai_compatible.py` (any
    self-hosted/third-party OpenAI-compatible endpoint, reusing the
    `openai` SDK itself pointed at a configurable `base_url`). A new
    `ai:` config section (`provider`/`model`/`base_url`/
    `timeout_seconds`) follows `core/config.py`'s existing layered-config
    conventions; API keys are read from environment variables
    (`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`RAGPILOT_AI_API_KEY` for
    `openai_compatible`) rather than stored in `config.yaml`/
    `.ragpilot.yaml`, both plain unencrypted files -- acceptable for CI
    per the blueprint; OS-credential-store integration (Credential
    Manager/Keychain/Secret Service) is a documented, deliberate gap, a
    larger separate undertaking outside this phase's scope.
  - **`privacy.external_ai_allowed` gating**: OpenAI/Anthropic/an
    arbitrary OpenAI-compatible endpoint always refuse with a clear
    `AiPrivacyBlockedError` unless this already-existing flag (defaults
    `false`) is explicitly `true`. Ollama is exempt from the flag *only*
    when its configured host actually resolves to loopback
    (`localhost`/`127.0.0.1`/`::1`) -- a request that never leaves the
    machine is not "external AI" in the sense the flag exists to gate,
    applying the blueprint's local-first framing literally. A non-default,
    remote Ollama `base_url` is **not** exempt: it is real network egress
    to a third party, no different in kind from the other three
    providers, so it is gated identically rather than inheriting Ollama's
    usual local-first pass -- a deliberate, narrower reading documented in
    `ai/factory.py` rather than left as an unstated assumption.
  - **`ragpilot ask "QUESTION" [--json]`**: orchestrates the exact same
    deterministic retrieval `ragpilot explore` uses (`retrieval/
    planner.py` + `cli/explore.py`'s own `_run`, not a second retrieval
    implementation) to assemble an evidence package, then hands the
    question and that evidence to the configured `ai:` provider for a
    synthesized answer, returned alongside the evidence it was based on
    so the answer stays auditable against real, already-indexed sources.
    Fails with a clear, actionable error -- never a silent no-op, never an
    unhandled traceback -- mapped onto `core/errors.py`'s existing exit
    codes: no provider configured (`AiNotConfiguredError`, reuses
    `ConfigError`'s exit code 3), the privacy gate blocks a cloud provider
    (`AiPrivacyBlockedError`, reuses `SecurityViolationError`'s exit code
    8), or the provider call itself fails -- network error, bad API key,
    non-2xx response (`AiProviderError`, exit code 1, the same generic-
    failure code any other unclassified CLI-boundary exception already
    gets). A new `ragpilot_ask` MCP tool (`mcp/tools.py`/`schemas.py`,
    following Phase 6's exact pattern) exposes the same command to an MCP
    client, registered with distinct (`openWorldHint=True`) annotations
    and its own instructions text since -- unlike the other 8 read-only,
    network-free tools -- it can reach a real, possibly cloud, endpoint.
  - **Testing**: zero real network calls in the default test suite for
    any AI provider -- every provider is proven with a dependency-injected
    `httpx.Client(transport=httpx.MockTransport(...))` covering success,
    an API error response, and a network failure, plus `ai/factory.py`'s
    privacy-gating logic (including the Ollama loopback-vs-remote
    distinction) proven without constructing a real HTTP client at all.
    A real local embedding model load is a genuine, first-run network
    dependency (Hugging Face weight download) exactly like Phase 3's
    Docling PDF pipeline, so it gets the identical treatment: a new
    `embedding_model` pytest marker, excluded from the default run
    (`pyproject.toml`'s `addopts`), confirmed by actually running
    `pytest -m embedding_model -q` (both real-model tests pass: correctly
    normalized 384-dim vectors, and semantically related sentences score
    higher cosine similarity than unrelated ones), plus a matching
    non-blocking `embedding-model-tests` CI job mirroring
    `docling-pdf-tests` exactly. Every other Phase 9 test -- vector
    storage/cosine similarity, the `embeddings` repository, semantic
    search's degrade-gracefully paths, and full `ragpilot index`/
    `search`/`explore`/`ask` integration coverage -- runs in the default
    suite using precomputed/fake vectors and a monkeypatched
    `embedder.embed_texts`, never the real model.

- One-line install scripts (`install.sh` for macOS/Linux, `install.ps1`
  for Windows), matching the `curl | sh` / `irm | iex` UX used by tools
  like `rustup`/`deno`. Both scripts assume Python 3.12+ is already on
  `PATH` -- they do not install Python itself -- and download the
  package source for a given ref (`RAGPILOT_REF`, default `main`) from
  GitHub, create an isolated virtual environment, `pip install` into it,
  and link/launch `ragpilot` from a per-user bin directory, printing (or,
  on Windows, applying) the `PATH` fix if that directory isn't already on
  it. Documented in `README.md`'s Installation section and
  `CONTRIBUTING.md`. A new, non-blocking `install-script-tests` CI job
  (matrixed across all three OSes) runs each script for real -- a genuine
  network fetch of the pushed branch plus a full `pip install` of the
  package, including torch/docling -- and verifies the resulting
  `ragpilot` launcher actually runs, mirroring the `docling-pdf-tests`/
  `embedding-model-tests` non-blocking pattern.

- PDF documents are now converted through a Markdown round-trip rather
  than normalized straight off Docling's PDF-layout output
  (`documents/docling_adapter.py`). After Docling's real PDF pipeline
  (layout/table-structure models) produces its `DoclingDocument`, it is
  exported to Markdown text (`export_to_markdown`, with a plain-text page
  break placeholder inserted at each page transition -- an HTML-comment-
  style placeholder is silently dropped by Docling's Markdown parser on
  reparse, so a literal marker string wrapped in U+2063 INVISIBLE
  SEPARATOR is used instead) and reparsed through Docling's own Markdown
  backend into a second `DoclingDocument`, and it is *that* document
  that actually gets normalized/chunked/indexed. The Markdown text is
  cached by the PDF's content hash in a new `document_conversion_cache`
  table (`storage/schema.py`'s additive `KNOWLEDGE_DB_V6`/migration 6,
  keyed by content hash rather than file id so a moved/renamed/
  duplicated PDF with identical bytes still hits the cache, versioned via
  a `cache_version` column so a future change to the marker or export
  options can't misinterpret old cached Markdown), so re-indexing an
  unchanged PDF never re-runs the expensive PDF ML pipeline again -- only
  the cheap Markdown reparse. Purely derived, disposable state, never
  actively pruned, mirroring `embeddings`' own precedent. A reparsed-
  from-Markdown document carries no page provenance on any item (empty
  `prov`, `num_pages() == 0`), so `docling_adapter.convert` now returns a
  `ConversionResult` (document plus an optional real page count and page-
  break marker, both `None` for every non-PDF format) and
  `normalizer.normalize` gained two keyword-only overrides to reconstruct
  page numbers by counting marker crossings instead of reading
  `item.prov`. Every other format (DOCX/PPTX/XLSX/HTML/Markdown/TXT/EML)
  is completely unaffected. Proven end to end with the real PDF pipeline
  under the `docling_pdf` marker: cache population, cache reuse (a second
  `convert()` call against the same connection is proven to never touch
  the PDF-pipeline singleton again), and the existing page-provenance
  golden test's assertions hold unchanged through the Markdown round-trip.

- Search Performance Redesign. Six phases replacing lexical full-corpus
  Python scans and semantic search's brute-force scan of every current-
  model embedding with indexed lookups and a persistent ANN index, while
  keeping every existing command's output contract backward-compatible
  (the full pre-existing test suite passes unmodified).
  - **Lexical fixes** (`storage/schema.py`'s additive migrations 7/8,
    `entities_repo.py`/`documents_repo.py`/`files_repo.py`): an indexed
    `entities.alias` column (`entities_repo.compute_alias`, backfilled
    for already-indexed rows via a recursive-CTE `UPDATE`) replaces the
    previous `list_all()` full-corpus alias scan; a
    `documents(title COLLATE NOCASE)` index replaces the equivalent
    full-corpus title scan; `retrieval/lexical.py`'s entity/document
    search now runs a single `JOIN files`/`JOIN documents` projection
    query per stage (new `EntitySearchRow`/`DocumentSearchRow`
    dataclasses) instead of a separate `files_repo.get()` round trip per
    hit. `retrieval/vectorstore.py`'s brute-force `top_k` now uses
    `heapq.nsmallest` (O(N log K)) instead of a full sort (O(N log N)).
  - **Path FTS** (migration 8): a `path_fts` FTS5 index over `files.path`
    (`files_repo.search_path_projection`) replaces `search_by_substring`'s
    `LIKE '%query%'` scan as the primary path-search path, falling back to
    the original `LIKE` scan for fragments that don't align to a token
    boundary.
  - **Persistent ANN semantic index** (new `usearch` dependency,
    `retrieval/ann.py`, `storage/repositories/vector_items_repo.py`,
    migration 9's `vector_items` table): a `usearch`-backed HNSW index
    (`AnnIndex` protocol, `USearchAnnIndex`) replaces the previous
    brute-force cosine scan over every current-model embedding as
    `retrieval/semantic.py`'s primary path, with a pure-Python
    `BruteForceAnnIndex` (same protocol) as an automatic fallback when
    `usearch` can't be loaded. `vector_items` maps compact integer vector
    ids to their code entity/document section, populated alongside
    `embeddings` during indexing (`indexing/embedding_indexer.py`); a
    batched `vector_items_repo.batch_metadata_lookup` replaces a
    per-search-hit metadata round trip. The index is synced
    incrementally per touched file right after each indexing pass
    (`indexing/runner.py`), auto-rebuilds once deleted vectors exceed
    `search.vector.rebuild_deleted_ratio` (default 15%), and any project
    with embeddings but no `vector_items` yet (pre-existing `knowledge.db`
    files, or the on-disk index's `model_id`/`dim` no longer matching)
    falls back to the original full-scan path unchanged, per source --
    no forced re-index is required. Adds `ragpilot vectors rebuild` and
    a `ragpilot doctor` "Semantic" section reporting the active backend
    and vector/index-size counts.
  - **Query routing** (`retrieval/query_classifier.py`): a deterministic
    (no AI) `classify_query`/`estimate_confidence`, and a new
    `search.lazy_semantic` config flag (default `false`, preserving
    today's "always attach a semantic section when `search.semantic` is
    on" contract) that skips semantic search entirely once the lexical
    pass already found a high-confidence hit. `ragpilot search --explain`
    surfaces the classified query kind, lexical confidence, and a
    per-stage timing breakdown.
  - **Hybrid reranking** (`retrieval/merger.py`, `retrieval/reranker.py`,
    `ragpilot search --hybrid`): dedups lexical and semantic hits by
    `(kind, id)` into one candidate set and reranks them into a single
    ordered list -- primary lexical tier, then semantic score, then the
    existing tie-breaker chain -- with a semantic-only hit always ranked
    below every lexical tier so similarity can never silently upgrade an
    exact match. Purely additive: the existing separate `results`/
    `semantic` sections are unchanged unless `--hybrid` is passed.
  - **Caching** (`retrieval/cache.py`, `search.cache.*` config): a
    bounded LRU cache for search results and for computed query
    embeddings, gated behind `search.cache.enabled` (default `true`).
    Both are process-global, since a one-shot CLI invocation gets no
    benefit from a cache it immediately discards -- the payoff is a
    long-lived process serving repeated queries within itself, chiefly
    `ragpilot serve`. Invalidation needs no explicit clear: every cache
    key folds in each searched database's file identity and its SQLite
    `PRAGMA data_version`, which changes whenever a different connection
    (every reindex opens its own) commits to it.
  - Not included in this pass: the blueprint's benchmark suite
    (`benchmarks/search/` across synthetic 5k-1M-embedding corpora) and
    golden-query quality regression tests (Recall@K/MRR/NDCG) are left
    for a follow-up -- everything else in the blueprint's Definition of
    Done is addressed above.

- Search Performance Redesign, follow-up: benchmark suite and golden-query
  quality regression tests (blueprint sections 35/36/37, the two items the
  previous entry above deferred).
  - **Benchmark suite** (new top-level `benchmarks/search/` package,
    `pytest -m benchmark_search`): a synthetic corpus generator
    (`corpus.py`) writes entities/documents/embeddings directly through
    the storage repositories -- no Tree-sitter/Docling parsing, no real
    embedding model -- at the blueprint's suggested sizes
    (`small`=5k/`medium`=50k/`large`=250k/`very_large`=1M embeddable
    subjects), always including one deterministic "known" entity/
    document/path so every query category has a guaranteed correct hit
    regardless of size. `queries.py` builds the blueprint's eight
    benchmark query categories against those known fixtures; `runner.py`
    measures p50/p95 latency (with `search.cache` disabled, so repeated
    calls measure the real pipeline, not a cache hit) and compares
    against `targets.py`'s section-36 targets. Query-time embedding uses
    a fast, deterministic hash-based stand-in (`fake_embedder.py`, the
    same technique `test_semantic_retrieval.py` already used) rather
    than the real model, so even the standalone
    `python -m benchmarks.search --size large` script never touches a
    network or a real ML model. Millisecond targets are reported, not
    hard-asserted, in the automated `pytest`/CI coverage (a shared or
    virtualized runner is not "normal developer hardware," the
    blueprint's own stated measurement condition); `--strict` on the
    standalone script is the form that actually enforces them, for a
    real machine. CI runs the fast `small` size in a separate,
    non-blocking job, the same style as `docling_pdf`/`embedding_model`
    (see CONTRIBUTING.md's new `benchmark_search` section).
  - **Golden-query quality regression test**
    (`tests/integration/test_search_quality.py`, always-on/default
    suite): indexes a small, fixed settlement/health-themed fixture
    project through the real CLI pipeline (real Tree-sitter parsing,
    unlike the latency benchmark's synthetic corpus) and evaluates every
    query in the new `benchmarks/search/golden_queries.yaml` (a uniform
    `{kind, path}` generalization of the blueprint's own
    `expected`/`expected_paths`/`expected_documents` examples) via
    binary-relevance Recall@5/@10, Mean Reciprocal Rank, and NDCG@10
    (`benchmarks/search/quality.py`). Deliberately lexical-only: the
    fixture's document text shares real vocabulary with its
    "conceptual"-style golden queries, so lexical FTS alone already
    answers them correctly, keeping this test offline and model-free.
    NDCG's discounted-gain sum only credits a relevant key's *first*
    occurrence -- multiple distinct entities retrieved from the same
    relevant file must count as finding that file once, not once per
    entity, otherwise NDCG could exceed its `[0, 1]` bound (caught by
    this same golden query set during development).
  - **Bug fix surfaced by the above**: `files_repo.py`'s path-FTS query
    builder OR'd every token together (favoring recall, matching
    `lexical.py`'s own content-search queries) -- but a path fragment's
    tokens routinely include the file extension (`"py"`, `"md"`, ...),
    and OR-ing a generic extension token in as its own clause matched
    *every* file of that type. Changed to AND: a multi-token path query
    now means "these tokens together," which a single-token query (the
    common case) behaves identically under either way.

- Search Quality Improvement Plan, Phase 0: a measurable benchmark
  baseline, captured *before* any extraction/ranking changes begin, for
  every later phase of the plan to diff its own work against.
  - **Expanded golden query set**: `benchmarks/search/golden_queries.yaml`
    grew from 8 to 72 queries, each tagged with a `category` -- the
    plan's nine required buckets (`exact_title_lookup`,
    `exact_heading_lookup`, `exact_symbol_lookup`, `keyword_search`,
    `semantic_document`, `table_question`, `cross_document`,
    `code_to_document`, `typo_partial_term`) plus `file_path_lookup`
    (present in the original 8-query set, kept as its own category since
    it exercises `retrieval/lexical.py`'s PATH tier specifically). Every
    query resolves against a real, extended fixture project -- new
    `benchmarks/search_quality/fixture_project.py` (three code files,
    three Markdown documents with headings and two tables), shared by the
    quality test and the new report generator below so the two can't
    silently drift out of sync. `expected` relevant sets are the
    reviewer's judgment of genuinely-relevant results, not "whatever the
    current top-5 happens to contain" -- several queries (documented
    inline) deliberately have a real, non-1.0 achievable recall today
    (e.g. a PATH-tier hit crowded out of the top 5 by document FTS
    noise), a genuine baseline gap for later phases to close rather than
    a query rewritten to hide it. `typo_partial_term` is honestly scoped
    to what FTS5's default tokenizer can already do (no fuzzy/edit-
    distance matching): fragment queries that fall through to the path
    LIKE fallback, and single-typo words riding along with correctly-
    spelled tokens in a multi-word OR query -- not a claim that spelling
    correction already works.
  - **`benchmarks/search_quality/`** (new top-level package, alongside
    `benchmarks/search/`'s existing latency suite): `evaluator.py` runs
    the golden set through the real `retrieval/lexical.search` and
    computes Recall@1/3/5/10, MRR, and NDCG@10
    (`benchmarks/search/quality.py`), overall and broken down per
    category -- the one evaluation both the quality test and the report
    generator below build on. `report.py`
    (`python -m benchmarks.search_quality`) produces the plan's full
    baseline report: golden-query quality; lexical/semantic/hybrid search
    latency p50/p95 and cold-vs-warm semantic search latency (reusing
    `benchmarks/search`'s existing synthetic-corpus latency infra);
    indexing time by file type, re-indexing (no-op) time, generated chunk
    count (code entities + document sections), vector count, and
    knowledge-DB/vector-index size on disk (measured directly against the
    fixture project, real Tree-sitter/Docling parsing). Its actual output
    is committed as `benchmarks/search_quality/baseline_report.json`/
    `.md` -- a real, run-once artifact, not just code that could produce
    one.
  - **Blocking quality gate**: `tests/integration/test_search_quality.py`
    (refactored onto the new evaluator, no pytest marker -- fast, offline,
    deterministic, runs in the default suite) now asserts both the
    overall Recall@1/3/5/10/MRR/NDCG@10 and each category's own Recall@5
    stay at or above this fixture's known-achievable floor (a per-
    category floor rather than one overall number, since a weak category
    can hide inside an otherwise-healthy overall average and a strong
    category's regression can hide inside a lenient overall floor) --
    "search changes cannot be merged without running the benchmark
    suite." True latency benchmarking stays non-blocking exactly as
    before: a new `tests/integration/test_search_quality_report.py`,
    marked `benchmark_search` like the existing latency suite, exercises
    the full report generator end to end without hard-gating on its
    hardware-dependent millisecond numbers.
  - No retrieval/indexing/chunking source code changed in this phase --
    benchmark infrastructure only.
- Search Quality Improvement Plan, Phase 1B: PDF documents are now
  normalized straight off Docling's real PDF-layout `DoclingDocument`
  instead of a document reparsed from that document's own Markdown
  export.
  - **Before**: `docling_adapter.convert` exported the real PDF
    pipeline's `DoclingDocument` to Markdown, cached that Markdown text
    by content hash, and reparsed it through Docling's separate Markdown
    backend into a *second* `DoclingDocument` -- the one actually
    normalized/chunked/indexed. That reparsed document carried no `prov`
    on any item, so `normalizer.normalize` reconstructed page numbers by
    counting a literal page-break marker string embedded in the Markdown
    export.
  - **After**: the real PDF pipeline's own `DoclingDocument` is cached
    directly, serialized via its pydantic model's native
    `model_dump_json()`, and a cache hit deserializes straight back via
    `model_validate_json()` -- no second Docling backend, no marker, no
    reconstruction. `normalizer.normalize` now reads page numbers off
    every format's real `prov` uniformly, PDF included; its
    `page_count_override`/`page_break_marker` parameters and the whole
    marker-reconstruction code path are gone.
  - `document_conversion_cache` (`storage/schema.py`'s `KNOWLEDGE_DB_V6`)
    is reshaped in place by a new additive migration, `KNOWLEDGE_DB_V10`:
    its `markdown` column is renamed to `serialized_document` (a pure
    catalog change -- no row's bytes are touched) and two new nullable
    columns, `serialization_format` and `parser_version`, are added.
    Per this project's documented migration strategy, no cache row
    written before this phase is converted -- `docling_adapter
    ._CACHE_VERSION` is bumped alongside the migration, so every such row
    (holding old Markdown text under its new column name) is treated as
    stale and simply regenerated, natively, the next time its PDF is
    indexed.
  - A cache hit now costs a single pydantic JSON deserialization instead
    of a full reparse through Docling's Markdown backend -- strictly
    cheaper, so re-indexing an unchanged PDF is at least as fast as
    before (measured locally: ~10s cold PDF-pipeline conversion vs.
    ~1.4ms warm cache-hit deserialization for this project's PDF test
    fixture).
  - `tests/unit/test_docling_pdf.py` (network-gated, `docling_pdf`
    marker) gained tests for a duplicate PDF with identical content
    reusing the cache, a moved/renamed PDF reusing the cache, and a
    stale `cache_version` forcing reconversion.
    `tests/unit/test_document_conversion_cache.py` gained a
    non-network-dependent test proving the JSON serialization scheme
    itself round-trips headings, paragraphs, tables, and page `prov`
    losslessly. `test_document_normalizer.py`'s
    `test_page_break_marker_reconstructs_page_numbers_without_prov` is
    removed (the mechanism it tested no longer exists) and replaced by
    `test_page_numbers_come_from_native_prov_not_a_marker`.
- Search Quality Improvement Plan, Phase 1A: safe, confirmed `ragpilot
  source remove`.
  - **The bug**: `remove` deleted only the source's row in `sources.db`,
    never its derived project directory (`knowledge.db`/`-wal`/`-shm`,
    `vectors.usearch`/`.meta.json`, `cache/`, `state/`) -- and did so with
    no confirmation prompt at all. The directory was left behind forever,
    and re-adding the exact same path later (same canonical path, same
    deterministic project id) silently reused that stale, already-indexed
    generation instead of starting clean.
  - `sources/registry.py`'s `SourceRegistry.remove` now deletes the
    entire project directory wholesale (`shutil.rmtree`, not an
    enumeration of known files -- so a derived artifact a later phase
    adds is covered automatically) and only removes the `sources` row
    once that succeeds; a filesystem failure leaves the source registered
    rather than half-removed. Reuses `core/paths.py`'s existing
    project-id/project-dir resolution (the same one `index`/`rebuild`
    use) rather than a new path scheme, and resets the process-local
    search-result/query-embedding caches (`retrieval/cache.py`) on
    success. Refuses outright, with an actionable
    "run `ragpilot daemon stop` first" error, while a daemon is running --
    failing fast rather than risking a race with its watcher/reconciliation
    threads or hanging behind the same `index.lock` a daemon pass holds.
  - `cli/source.py`'s `remove` now shows the source id, path, and the
    project directory that will be deleted, then prompts for
    confirmation (only `y`/`yes` proceeds; anything else, a bare Enter, or
    EOF/Ctrl+C leaves everything untouched) -- `--yes`/`-y` skips only the
    prompt, for non-interactive use. Acquires the same `index` lock
    `index`/`rebuild` already take before touching anything, keeping the
    CLI layer to confirmation/presentation only, with the actual
    removal semantics in the registry.
  - The original source files on disk are never touched, only derived
    data.
- Search Quality Improvement Plan, Phase 2: token-aware, hierarchy-aware
  chunk boundaries.
  - **The problem**: `documents/chunker.py` grouped paragraph units into
    chunks by a flat character count (`DEFAULT_MAX_CHUNK_CHARS`), with no
    concept of tokens, a token budget, or a floor -- a "1000-character"
    chunk could be anywhere from ~150 to ~500 real tokens depending on
    word length and punctuation density, and a lone short paragraph under
    a heading always became its own tiny standalone row regardless of
    size.
  - **`documents/tokenization.py`** (new): a small, dependency-free
    word/punctuation token estimator (`count_tokens`) plus a sentence-
    then-word-boundary budget splitter (`split_by_token_budget`), used in
    place of the real embedding-model tokenizer -- documented in the
    module's own docstring why: loading `retrieval/embedder.py`'s
    `transformers.AutoTokenizer` downloads/caches model files from
    Hugging Face on first use (the same real-network dependency
    `CONTRIBUTING.md`'s `embedding_model` marker exists to keep out of
    the default test suite), and chunk *boundaries* have to keep working
    even when the embedding model is entirely unavailable (FTS-only
    indexing, `EmbeddingModelUnavailableError`) -- coupling boundary
    decisions to that model being loadable would regress that
    degrade-gracefully guarantee for every document, not just semantic
    search.
  - **`core/config.py`**: new `ChunkingConfig`
    (`documents.chunking.{strategy,max_tokens,min_tokens,overlap_tokens,
    merge_peers}`, defaults `hybrid`/350/60/40/`true`) under
    `DocumentsConfig`, validated the same way `SearchOutputConfig`
    already is (`strategy` restricted to the one implemented value,
    `min_tokens <= max_tokens`, `overlap_tokens < max_tokens`). Threaded
    through `indexing/coordinator.py`'s `ProcessorContext` (a new
    `chunking` field, mirroring `max_document_pages`) to
    `documents/pipeline.py`'s call into `chunk_document`.
  - **`documents/chunker.py`**: paragraph units under one heading are now
    greedily packed by real token count up to `max_tokens` instead of
    `max_chunk_chars`; a single unit whose own text exceeds `max_tokens`
    is split at sentence, then word, boundaries (never a raw character
    cut) before packing. `overlap_tokens` worth of trailing text seeds
    the next chunk -- structurally scoped to one heading's own
    contiguous run of paragraph units (`flush_pending`), so it can never
    bleed into a different heading's first chunk. `merge_peers`
    rebalances (not just concatenates -- adjacent greedily-packed groups
    are, by construction, always too large to simply recombine; see the
    function's own docstring) any adjacent pair where one side came out
    under `min_tokens`, without ever exceeding `max_tokens` on either
    side; when a pair's combined content genuinely can't support two
    full-sized floors, the shortfall is left as-is (`min_tokens` is a
    soft target, not a hard floor -- `max_tokens` is the one hard
    ceiling). Tables remain the one deliberate, documented exception:
    never split, so a table's `token_count` can legitimately exceed
    `max_tokens`.
  - `Chunk` grew `contextual_text` (heading-path-prefixed rendering of a
    chunk's text, or of a table's caption/flattened cells) and
    `token_count` (the real count the boundary decision was based on).
    Neither is wired into FTS/embedding indexing yet -- Phase 3 decides
    what actually feeds search from raw vs. contextual text; this phase
    only makes both available on every chunk. Existing fields
    (`text`/`heading_path`/`heading_level`/`parent_index`/`page_start`/
    `page_end`/`table_rows`) are unchanged/unrenamed.
  - **Table captions**: `documents/normalizer.py` now resolves a table's
    Docling-associated caption (`TableItem.captions`) to its own unit's
    new `caption` field, and drops the caption's own `TextItem` from the
    unit list entirely -- it no longer also survives as an unrelated
    stray paragraph placed right after the table.
  - Verified against `benchmarks/search_quality/baseline_report.json`: the
    golden-query Recall@1/3/5/10/MRR/NDCG@10 numbers and generated chunk
    counts are byte-identical to the committed Phase 0 baseline on that
    fixture project (its documents are small enough that no boundary
    actually moved) -- `tests/integration/test_search_quality.py`'s
    blocking floors pass unchanged, and the baseline did not need
    regenerating.
- Search Quality Improvement Plan, Phase 3: `search_text`/`embedding_text`
  wired into FTS and embedding indexing.
  - **The problem**: Phase 2 computed `Chunk.contextual_text` from
    `heading_path` alone (the document's title wasn't known that early in
    the pipeline) and never wired it anywhere -- `retrieval/lexical.py`'s
    `document_fts` indexed each row's raw `text`, and
    `indexing/embedding_indexer.py` embedded each `document_sections`
    row's raw, context-free `text` too. An isolated chunk's own words
    often don't say which document/section they're from (e.g.
    "Settlement" alone doesn't say *which* settlement flow), which is
    exactly the ambiguity a similarity model needs resolved.
  - **`documents/chunker.py`**: `chunk_document` now takes an optional
    `doc_title`, threaded into two formatted views computed on every
    `Chunk` alongside `contextual_text`/`token_count` (`text`/`raw_text`
    itself is unrenamed and unchanged -- the clean body always returned
    as evidence):
    - `search_text` (new field): document title, then each `heading_path`
      segment, then `text` -- one per line. Lexical-search-oriented: a
      query phrased against title/heading vocabulary absent from the raw
      body now matches on the lexical pass.
    - `contextual_text` (Phase 2 field, reshaped): now also carries
      `doc_title` -- `"Document: <title>\nSection: <heading path>\n\n<text>"`
      -- rather than heading-path-only. This is the chunk's embedding
      input.
    Both degrade gracefully line-by-line when title/heading_path/body are
    partially or entirely absent (e.g. a synthetic unit test's
    heading-less, title-less chunk still gets a well-formed, non-empty
    value back).
  - **`documents/pipeline.py`**: `extract_metadata` now runs *before*
    `chunk_document` (it never depended on the chunk list, only on
    `conversion.document`/`normalized`) so the document's title is known
    in time to pass as `chunk_document`'s new `doc_title`.
  - **`storage/repositories/documents_repo.py`**: `insert_section`/
    `insert_paragraph`/`insert_table` gained optional `search_text`/
    `embedding_text` keyword args. `documents/pipeline.py` (the real
    indexing path) always passes `Chunk.search_text`/`Chunk.
    contextual_text`; every other existing direct-insert caller (unit
    tests, the synthetic-corpus latency benchmark) that doesn't pass them
    keeps its previous `document_fts` body content unchanged, so none of
    them needed updating. `search_text` becomes `document_fts`'s indexed
    body column (replacing the raw text it held before); `embedding_text`
    is persisted verbatim on the row's own `document_sections.embedding_text`
    (new nullable column, `KNOWLEDGE_DB_V11`, additive/no-backfill --
    `KNOWLEDGE_DB_V7`'s precedent) since `indexing/embedding_indexer.py`
    reads already-stored rows, not live `Chunk` objects, so this is the
    one seam wide enough to carry the embedding-time text forward.
    `documents_repo.DocumentUnit` grew a matching `embedding_text` field
    (`""` when unset, never `None`, so every reader can treat it
    uniformly).
  - **`indexing/embedding_indexer.py`**: `embed_touched_files` now embeds
    each document-section subject's `embedding_text` (falling back to its
    raw `text` only when `embedding_text` is unset -- a row written before
    this migration and not yet reindexed) instead of always embedding raw
    `text`. This is the actual natural-language/semantic-retrieval
    improvement this phase targets; verified directly (real embedder
    still never loaded in the default suite) in the new
    `tests/unit/test_embedding_indexer.py`.
  - Verified against `benchmarks/search_quality/baseline_report.json`:
    golden-query Recall@1/3/5/10/MRR/NDCG@10 (including the
    `semantic_document` category) and generated chunk/vector counts are
    byte-identical to the committed baseline -- that evaluation is
    deliberately lexical-only (`retrieval/lexical.search`) against a
    fixture small enough that the pre-existing separate `heading_text`/
    `doc_title` FTS columns already gave every golden query's expected
    row a matching signal, so wiring `search_text` into the shared `body`
    column moved nothing measurable on this particular fixture; the
    `embedding_text` change (semantic retrieval) isn't exercised by that
    lexical-only script at all, and is instead covered by
    `tests/unit/test_embedding_indexer.py` and
    `tests/integration/test_document_indexing.py`'s new assertions.
    `tests/integration/test_search_quality.py`'s blocking floors pass
    unchanged, and the baseline did not need regenerating (mirrors Phase
    2's own "byte-identical, no regeneration needed" precedent).
- Search Quality Improvement Plan, Phase 4: row-aware table extraction.
  - **The problem**: `documents_repo.insert_table` indexed a table as one
    space-joined blob of every cell (`" ".join(cell for row in table_rows
    for cell in row)`) -- a table's cells' row association was lost the
    moment they were flattened, so a query like "which server has 85%
    CPU?" had no way to tell that `api-02` and `85%` came from the same
    row rather than merely both appearing somewhere in the same table.
    Separately, a table's caption (Phase 2's `caption` field) was
    computed but never actually persisted or indexed anywhere, and a
    table was never split regardless of size, however large.
  - **`documents/table_renderer.py`** (new): renders a table's row-major
    grid as one pipe-delimited line per row (`render_rows`/
    `render_table`, caption prepended when present) instead of a
    flattened blob, so the tokens that were in the same row stay on the
    same line for both FTS5 and an embedding model. `split_data_rows`
    splits a table's data rows at row boundaries only -- never mid-row --
    into groups that each fit a token budget once its header rows and any
    fixed overhead (e.g. the caption) are counted in; a single row that
    alone still exceeds the budget is kept whole (there is no meaningful
    sub-row unit to cut at).
  - **`documents/normalizer.py`**: `NormalizedUnit` grew
    `header_row_count`, resolved per table from Docling's own per-cell
    `TableCell.column_header` metadata when the backend populated it
    (the maximal contiguous run of header rows starting at row 0, so a
    merged/multi-row header is counted correctly), falling back to
    "row 0 is the header" for hand-authored Markdown/HTML/DOCX tables
    whose backends never run that model at all.
  - **`documents/chunker.py`**: a table whose row-aware rendering exceeds
    `max_tokens` is now split into multiple table chunks at row
    boundaries, each repeating the table's header rows at the top
    (`table_renderer.split_data_rows`) so every resulting chunk stays
    independently interpretable -- e.g. a 60-row table becomes several
    chunks of "header + a handful of data rows" instead of one giant,
    over-budget chunk. A table that already fits `max_tokens` is still
    emitted as exactly one chunk, unchanged from before. The one
    remaining atomic exception is now row-scoped rather than
    table-scoped: a single row too large to fit alongside its own
    repeated header stays in its own one-row chunk.
  - **`storage/repositories/documents_repo.py`**: `insert_table` now
    indexes `table_renderer.render_table`'s row-aware rendering as both
    the table's FTS `body` and its `document_sections.text` (the same
    column `embed_touched_files` reads for embedding text), replacing the
    flattened-cell blob -- exactly mirroring how `Paragraph.text` already
    feeds both. Heading-path/document-title context still reaches search
    the same way it always has, via the existing `fts_heading`/
    `doc_title` FTS columns, rather than being duplicated into the
    table's own text (a table-only special case for that would both
    diverge from every other chunk kind's `text` and risk
    `knowledge/linker.py` cross-domain-link false positives, since it
    substring-matches entity names against this same text).
  - **`core/models.py`**/**schema migration `KNOWLEDGE_DB_V12`**:
    `Table` grew a `caption` field, now actually persisted (a new
    nullable `document_sections.caption` column, additive `ALTER TABLE`,
    no backfill) instead of being silently dropped between chunking and
    storage as before -- mirrors how `heading_path` is both a raw column
    and separately materialized into FTS. (Numbered `V12`, not `V11`:
    Phase 3's `embedding_text` column landed first and claimed `V11`.)
  - New `documents/table_renderer.py` unit tests, `documents/chunker.py`
    table-splitting/header-repetition tests, a `documents_repo`/
    `document_fts` round-trip test, and an end-to-end integration test
    (`tests/integration/test_table_search.py`) indexing a small table and
    a 60-row table through the real CLI and proving: a CPU/RAM value
    query resolves to the row it actually belongs to; a large table
    splits into multiple chunks each carrying the header row; a term only
    present in a later split's rows is still found; and a query combining
    two facts only true of the same row (not merely present somewhere in
    the table) resolves to that row's own chunk.
  - Verified against `benchmarks/search_quality/baseline_report.json`:
    the `table_question`-category (and every other category's) golden-
    query Recall@1/3/5/10/MRR/NDCG@10 numbers are byte-identical to the
    committed baseline (its two table fixtures are small enough to stay
    one chunk each, so ranking is unaffected) -- the baseline did not
    need regenerating.
- Search Quality Improvement Plan, Phase 5: OCR auto-fallback for
  scanned/image-only PDFs.
  - **The problem**: `documents.ocr` (`config.py`) has existed since
    Phase 3 but was inert -- `docling_adapter.py` forced
    `pdf_options.do_ocr = False` unconditionally, so a scanned/image-only
    PDF indexed with zero extracted text and no way to recover it.
  - **`documents.ocr`: `"off"` | `"auto"` (default) | `"always"`**,
    validated in `core.config.DocumentsConfig`. `"off"` is byte-identical
    to every prior phase's only behavior. `"auto"` runs the plain,
    non-OCR pipeline first and only re-runs conversion with OCR enabled
    when that result looks too textless to be real body content.
    `"always"` skips the detection pass entirely and always runs OCR --
    cheaper than `"auto"` when the caller already knows it wants OCR,
    since it never pays for two pipeline runs.
  - **Auto-detection** (`normalizer._is_low_text_density`, a new
    `SCANNED_CHARS_PER_PAGE_THRESHOLD = 50` constant): OCR triggers when
    a PDF has no extracted text at all, fewer than 50 extracted
    characters per page on average, or most pages produced no text item
    at all. This same function now also backs `normalize`'s `is_scanned`
    flag, replacing its previous, simpler `total_text_chars == 0` check
    (Phase 3) -- `is_scanned` now reports "this document still looks
    textless" against whatever document actually got normalized (OCR'd
    or not, depending on `documents.ocr`), not just "OCR was never
    attempted". One definition of "does this look scanned", not two.
  - **`docling_adapter.py`**: a second, independent `DocumentConverter`
    singleton (`_get_ocr_converter`, `do_ocr=True`) alongside the
    existing plain one -- OCR is never toggled on the shared plain
    converter, so a request for OCR can never change what every other
    caller's pipeline does. `convert()`/`_convert_pdf` gained an
    `ocr_mode` parameter (keyword-only, defaulting to `"off"` so every
    pre-Phase-5 caller, tests included, is unaffected); real callers get
    the configured mode via `indexing/coordinator.py` threading
    `config.documents.ocr` through `ProcessorContext.ocr` (mirroring
    `chunking`/`max_document_pages`) to `documents/pipeline.py`. An OCR'd
    PDF still produces a plain `DoclingDocument`, normalized/chunked/
    stored through the exact same downstream pipeline as any other --
    no parallel OCR-specific code path.
  - **Caching**: `document_conversion_cache` (`storage/schema.py`'s
    `KNOWLEDGE_DB_V6`/`V10`) gains an `ocr_used` (`"off"`/`"on"`) column
    that joins `content_hash` as the table's key (`KNOWLEDGE_DB_V13`,
    additive: recreates the table under a temporary name and backfills
    every pre-existing row as `ocr_used = 'off'`, since every row cached
    before this phase was, unambiguously, produced by the plain
    pipeline -- unlike `KNOWLEDGE_DB_V10`'s "bump cache_version, let old
    rows go stale" precedent, nothing here needs to be discarded).
    `document_conversion_cache_repo.get`/`put`/`CachedConversion` gained
    a required `ocr_used` field/parameter. A page cached from a plain
    conversion is never handed back for a request that actually needed
    OCR, and vice versa; `"auto"` transparently shares `"off"`'s cache
    entry when it doesn't trigger OCR, and `"always"`'s when it does.
  - **OCR engine**: this project's pinned `docling` range (`>=2.40,<3`)
    already transitively installs RapidOCR via `docling-slim[standard]`
    (see `pyproject.toml`'s updated dependency comment) -- no new
    dependency was added for this phase. Regardless,
    `docling_adapter._run_ocr_conversion` degrades gracefully (catches
    any OCR-specific failure, logs a warning, and callers fall back to
    the plain conversion) if a given environment's OCR engine is
    missing, broken, or can't reach its model weights -- verified with a
    dedicated graceful-degradation test that never touches Docling's
    real pipeline.
  - **Tests**: `tests/unit/test_docling_adapter_ocr.py` (new, default
    suite, no model download) covers every `"off"`/`"auto"`/`"always"`
    branch, both cache-key-distinguishing directions, and graceful
    degradation, entirely mocked at the `_get_converter`/
    `_get_ocr_converter`/`_run_conversion` seam.
    `tests/unit/test_document_normalizer.py` pins
    `_is_low_text_density`'s threshold behavior directly plus
    `normalize`'s `is_scanned` wiring. `tests/unit/
    test_document_conversion_cache.py` covers `ocr_used` as a genuine
    cache key at the storage layer. `tests/unit/test_docling_pdf.py`
    (`docling_pdf`-marked) adds a real, end-to-end proof -- a new
    `scanned.pdf` fixture (a hand-assembled PDF with an empty content
    stream, standing in for a true scanned/image-only PDF without this
    project needing an image-embedding fixture library) -- that `"auto"`
    really detects a textless PDF and re-runs Docling's real OCR
    pipeline (RapidOCR) against it, populating both cache rows.
- Search Quality Improvement Plan, Phase 6: tiered lexical query
  planning.
  - **The problem**: `retrieval/lexical.py`'s FTS query builder had
    exactly one tier -- every query token quoted and OR'd together
    (`"delayed" OR "settlement" OR "provider"`) -- so a multi-word query
    scored a document sharing just one token identically (as far as
    ranking was concerned) to one containing the full phrase, and only
    `_sort_key`'s bm25-rank tiebreaker separated them within the same
    `RankTier.FTS` bucket.
  - **`retrieval/lexical.py`**: new `LexicalQueryPlan`/
    `LexicalQueryVariant` dataclasses build four candidate FTS5 MATCH
    expressions per query, most-precise first -- Tier A exact phrase
    (`"delayed settlement provider"`), Tier B all terms ANDed
    (`"delayed" AND "settlement" AND "provider"`), Tier C a selective
    prefix fallback (only tokens >= 4 chars get `word*`-wildcarded, and
    only when that actually differs from Tier B, so a query of only
    short tokens earns no Tier C at all), and Tier D today's permissive
    OR fallback as the last-resort safety net. New `LexicalTier` enum
    (`PHRASE`/`ALL_TERMS`/`PREFIX`/`OR_FALLBACK`) tiers this
    query-*structure* precision -- a second axis entirely orthogonal to
    the existing `RankTier` (which tiers *signal kind*, e.g. exact
    symbol vs. FTS; see the module docstring for how the two compose).
    `_run_query_plan` executes a plan's variants in order and stops as
    soon as a tier has already surfaced enough distinct results, so
    Tier C/D only ever cost a query when Tier A/B's exact-phrase/
    all-terms match came up short; an identical expression to one
    already tried (always true for a single-token query, where every
    tier collapses to the same one-token match) is skipped rather than
    re-run, so single-token/identifier lookups fire exactly the one FTS
    query they always did. `SearchResult` grew a `query_tier` field
    (default `PHRASE`, so every non-FTS result kind is unaffected);
    `_sort_key` now breaks ties within a `RankTier` bucket by
    `query_tier` before `fts_rank`, so a hit found by multiple tiers
    (`_merge`'s existing (kind, id) dedup) always keeps its *strongest*
    tier rather than whichever tier happened to run last.
  - **`retrieval/merger.py`**/**`retrieval/reranker.py`**: `SearchCandidate`
    carries the winning `lexical_query_tier` through hybrid merge, and
    `reranker._sort_key` includes it as a tiebreaker too, so
    `ragpilot search`'s hybrid (lexical + semantic) path gets the same
    phrase-over-OR-noise ordering as plain lexical search, not just
    `retrieval/lexical.search` itself.
  - New `tests/unit/test_lexical.py` coverage: `LexicalQueryPlan`
    construction for single-token (Tier A/B/D collapse to one query,
    Tier C never built) and multi-word queries (all four tiers, in
    order); `_run_query_plan`'s early-stop and full-fallthrough
    behavior via a call-counting stub; and an end-to-end DB-backed test
    proving a document matching the full phrase outranks a same-tier
    document the OR-fallback alone found.
  - Verified against `benchmarks/search_quality/baseline_report.json`
    (regenerated -- numbers changed): overall MRR 0.8542 -> 0.9028 and
    NDCG@10 0.8833 -> 0.9158; `keyword_search` MRR 0.8125 -> 0.9375
    (Recall@1 0.5625 -> 0.8125); `cross_document` MRR 0.6875 -> 0.9375;
    `table_question` MRR/NDCG@10 both reach 1.0. Every
    exact_symbol_lookup/exact_title_lookup/exact_heading_lookup number
    stays at a perfect 1.0, `code_to_document`/`semantic_document`/
    `file_path_lookup`/`typo_partial_term` are byte-identical (none of
    those categories' queries are both multi-word and lexically
    resolvable, so the new tiers had nothing to change for them), and
    `tests/integration/test_search_quality.py`'s blocking floors
    (overall and per-category) still pass.
- Search Quality Improvement Plan, Phase 7: weighted BM25 column scoring
  for document search.
  - **The problem**: `documents_repo.search_fts`/`search_fts_projection`
    called FTS5's `bm25(document_fts)` with no column-weight arguments, so
    a term's incidental appearance several times in an unrelated
    paragraph's `body` could outscore the same term appearing once in the
    document's own `doc_title` or a section's `heading_text` -- title and
    heading are a much stronger relevance signal than raw body term
    frequency, and the scoring gave them no credit for that.
  - **`documents_repo.py`**: new `_DOCUMENT_FTS_COLUMN_WEIGHTS` constant
    (`doc_title=8.0`, `heading_text=5.0`, `body=1.0`, the search-quality
    plan's benchmark-driven starting point) passed as `bm25(document_fts,
    w1, w2, w3, w4, w5)`'s five positional arguments -- one per
    `document_fts` column *in its actual `CREATE VIRTUAL TABLE`
    declaration order* (`section_id`, `document_id`, `heading_text`,
    `body`, `doc_title`; the first two are `UNINDEXED` placeholders, kept
    in the tuple purely to hold their position), not the more intuitive
    title/heading/body reading order -- see the constant's docstring for
    why getting that order wrong would silently misweight the wrong
    column. Both `search_fts` and `search_fts_projection` (the function
    `retrieval/lexical.py`'s `_search_documents` actually calls) now build
    their `ORDER BY` from the same `_BM25_DOCUMENT_FTS_EXPR`, so a direct
    repository caller and the real search path always score identically.
    `code_fts`/`entities_repo.py` deliberately left untouched: an exact
    entity name/qualified-name match already ranks above any FTS hit via
    its own `RankTier.EXACT_SYMBOL`/`QUALIFIED_SYMBOL` tier (not `code_fts`
    scoring at all), so `code_fts`'s `name`/`qualified_name`/`snippet`
    columns don't carry the same "structural title vs. incidental body"
    distinction `document_fts` does -- no schema/config change either,
    per the plan's own "hardcode first, expose weights in configuration
    only after stable defaults are proven" sequencing.
  - New `tests/unit/test_lexical.py` coverage: an end-to-end DB-backed
    test proving a document whose title alone carries the query term
    outranks a same-tier document where the term appears five times,
    incidentally, in an unrelated paragraph's body; and a composition
    regression test proving a genuine exact-phrase match still outranks a
    weaker OR-fallback-tier match even when that weaker match's one shared
    token sits in the heavily-weighted `doc_title` column -- `_sort_key`
    orders by `LexicalTier` ahead of `fts_rank`, so Phase 6's tiered query
    planning and this phase's column weighting compose rather than fight.
  - Verified against `benchmarks/search_quality/baseline_report.json`
    (not regenerated -- every category's numbers are byte-identical to
    the committed Phase 6 baseline): `exact_title_lookup`/
    `exact_heading_lookup` stay at a perfect 1.0 across every metric
    (already at ceiling on this fixture, pre- and post-weighting), and
    `keyword_search`/every other category shows no regression.
    `tests/integration/test_search_quality.py`'s blocking floors still
    pass.
- Search Quality Improvement Plan, Phase 8: real Reciprocal Rank Fusion
  for the hybrid tier -- **deliberately reverses this project's own
  previous, documented design**, per explicit user direction, the same
  way Phase 1B's PR described its own reversal.
  - **The previous design, and why it's being reversed now**:
    `retrieval/reranker.py`'s module docstring used to state the reason
    it rejected RRF outright: a candidate found only via semantic search
    sorted into its own tier strictly below *every* lexical tier
    (`_SEMANTIC_ONLY_TIER`), and a candidate found by both kept its
    lexical tier as the sole primary signal, semantic score folded in
    only as a same-tier tie-breaker -- "never let semantic similarity
    silently upgrade an exact lexical tier." That rule is now too broad:
    it also blocked a strong semantic match from ever beating a weak,
    non-exact lexical hit (`RankTier.FTS`/`RankTier.PATH`), which is
    exactly the case this phase exists to fix. The exact-match guarantee
    itself is kept, narrowed to a new, explicit pinned tier (below).
  - **New `retrieval/fusion.py`**: the RRF math and this phase's
    candidate budgets, kept independent of `SearchCandidate` (and so of
    `merger.py`) to avoid a circular import and to stay trivially
    unit-testable against plain integers/floats. `RRF_K = 60` (the
    plan's suggested starting point, a named constant rather than
    scattered magic numbers) backs `rrf_score(lexical_rank,
    semantic_rank, *, k=RRF_K)`: `1/(k+lexical_rank) + 1/(k+
    semantic_rank)`, each term included only when that rank is known --
    a candidate found by only one signal still gets a valid score from
    that term alone, never dropped or penalized for lacking the other.
    Also home to the plan's candidate-budget constants:
    `MAX_LEXICAL_CANDIDATES`/`MAX_SEMANTIC_CANDIDATES` (50 each) and
    `MAX_FUSION_CANDIDATES` (100).
  - **`retrieval/merger.py`**: `SearchCandidate` grows `lexical_rank`,
    `semantic_rank`, `bm25_score`, `exact_match`, and `rrf_score` --
    additive fields alongside the existing `lexical_tier`/
    `semantic_score`/etc., nothing renamed or repurposed. `merge` now
    assigns `lexical_rank`/`semantic_rank` as each candidate's 1-based
    position within its own signal's (budget-capped) result list --
    `lexical_results` trusted in caller-given order (re-deriving
    `lexical.py`'s own multi-key tie-break chain here would duplicate
    logic that module's docstring already warns against), `semantic_hits`
    defensively re-sorted by score descending first so a caller's input
    order can never silently invert semantic ranks. `exact_match` is
    `True` exactly for the **pinned tier**: `RankTier.EXACT_SYMBOL`/
    `QUALIFIED_SYMBOL`/`ALIAS_SYMBOL`/`TITLE_OR_HEADING` (an exact
    document title or heading match) -- precisely the tiers the plan's
    own "preserve exact-match safety" language points at, and precisely
    what `tests/integration/test_search_quality.py`'s
    `exact_symbol_lookup`/`exact_title_lookup`/`exact_heading_lookup`
    floors already pin. `RankTier.FTS`/`RankTier.PATH` are **not**
    pinned -- they now fuse.
  - **`retrieval/repositories/documents_repo.py` /
    `entities_repo.py`**: `DocumentSearchRow`/`EntitySearchRow` (and
    `lexical.SearchResult`) grow a `bm25_score` field carrying the raw
    `bm25()` value FTS5 already computes for `ORDER BY` -- previously
    computed and then immediately discarded in favor of an ordinal
    `fts_rank` (`enumerate(rows)`). No query/scoring change: same
    `bm25(document_fts, ...)`/`bm25(code_fts)` expressions, just no
    longer thrown away, so it survives onto `SearchCandidate.bm25_score`
    as its own preserved signal (never consumed by `_sort_key` anywhere
    -- informational only, part of Phase 8's "preserve every signal
    separately, never overwrite" candidate shape).
  - **`retrieval/reranker.py`**: `rerank` now splits candidates into the
    pinned tier (sorted by the exact same tie-break chain as before --
    unconditionally first, untouched by fusion) and the hybrid tier
    (`RankTier.FTS`/`PATH`/semantic-only), which is ordered by
    `fusion.rrf_score` descending, capped at
    `fusion.MAX_FUSION_CANDIDATES`, with the same lower-priority
    tie-breakers as a last resort for an exact score tie. A hybrid-tier
    hit's `tier_label` is now `"hybrid"` (replacing the previous
    per-origin `"fts"`/`"path"`/`"semantic_only"` labels for that group,
    since they are no longer strictly tiered against each other) --
    pinned-tier labels (`"exact_symbol"` etc.) are unchanged.
    `RankedHit.to_dict()` gains an `rrf_score` key (rounded, `None` for
    pinned candidates, which are never fused).
  - **Tests**: new `tests/unit/test_fusion.py` (the RRF formula in
    isolation: both/either/neither rank present, custom `k`, monotonic
    rank ordering, "found by both beats found by one"). `tests/unit/
    test_merger_and_reranker.py` gains coverage for rank/budget
    bookkeeping, the pinned/hybrid split, and a hand-computed RRF
    example. **Deliberately changed, not silently deleted**:
    `test_rerank_never_lets_semantic_outrank_a_lexical_tier` (asserted
    the exact old behavior this phase reverses, using `RankTier.PATH` --
    not a pinned tier) is replaced by
    `test_pinned_tier_always_outranks_hybrid_tier_regardless_of_semantic_score`
    (same intent, now against an actually-pinned `RankTier.EXACT_SYMBOL`
    candidate, which still always wins) and
    `test_hybrid_tier_lets_a_strong_semantic_match_outrank_a_weak_lexical_match`
    (the new behavior: a lexical hit at rank 50 of 50 loses to a
    semantic-only hit at rank 1, by real RRF arithmetic, not a
    coincidental tie-break). `tests/integration/
    test_semantic_retrieval.py`'s `test_hybrid_flag_merges_and_reranks_
    without_changing_existing_keys` needed no assertion changes (its
    query's top hit is an exact symbol match, so it lands in the pinned
    tier either way) -- only its docstring was corrected to stop
    implying lexical tiers always outrank semantic ones in general.
  - **Candidate budgets**: reuses existing config surfaces for what
    actually gets fetched from the DB/ANN index (`--limit`,
    `SearchConfig.semantic_top_k`) rather than adding a parallel,
    redundant set of knobs; `fusion.py`'s
    `MAX_LEXICAL_CANDIDATES`/`MAX_SEMANTIC_CANDIDATES`/
    `MAX_FUSION_CANDIDATES` are enforced as upper-bound caps inside
    `merger.merge`/`reranker.rerank` on top of whatever a caller passes
    in, exercised directly by feeding oversized candidate lists in the
    new unit tests.
  - **Verified against `benchmarks/search_quality/baseline_report.json`
    (not regenerated -- every number is byte-identical)**: this
    benchmark's golden-query evaluator (`benchmarks/search_quality/
    evaluator.py`) calls `retrieval/lexical.search` directly and always
    has (see its own module docstring: "Deliberately lexical-only... 
    semantic/hybrid search has its own separate, already-covered
    contract") -- it never exercises `merger.py`/`reranker.py`/
    `fusion.py` at all, so a change confined to the hybrid ranking path
    cannot move its numbers, and none did:
    `exact_symbol_lookup`/`exact_title_lookup`/`exact_heading_lookup`
    stay at a perfect 1.0 across every metric, and `semantic_document`
    (already at a perfect Recall@5 = 1.0 on this fixture even
    lexical-only, by that category's own design -- see `golden_queries.
    yaml`'s header) is unchanged too. The phase's actual hybrid-tier
    reordering is verified instead by the new fusion/merger/reranker
    unit tests (a hand-computed reversal example) and the existing
    `--hybrid` integration coverage. The synthetic-corpus latency
    section's `hybrid` mode *does* exercise this path
    (`benchmarks/search/runner.py`) and stayed within measurement noise
    (p50 15.652ms -> 15.9ms, p95 16.176ms -> 16.745ms) -- pure rank
    arithmetic over already-computed lists, as expected.
- Search Quality Improvement Plan, Phase 9: matched-chunk context
  expansion (nearest parent heading, previous/next sibling chunks).
  - **The problem**: a document search hit's `snippet` is a single
    FTS5-extracted excerpt of the one matched paragraph/table chunk --
    a reader often needs the heading it falls under, or the sentence
    just before/after it, to actually interpret the hit, and had no way
    to get that without a separate lookup.
  - **Strictly post-ranking**: expansion only ever runs on a chunk a
    ranker has *already* selected and positioned -- it has no way to
    change what was found or its order, only what gets shown alongside
    it. Verified against `benchmarks/search_quality/baseline_report.json`:
    every golden-query metric, overall and per-category, is byte-identical
    with expansion on vs. off.
  - **`storage/repositories/documents_repo.py`**: new
    `get_chunk_neighbors` reuses the existing `parent_id`/`order_index`
    columns rather than adding any new storage -- a chunk's nearest
    heading is simply the row its own `parent_id` points at (every
    `parent_id` a non-heading chunk carries is a `HEADING` row's id --
    see `documents/chunker.py`), and its siblings are every row sharing
    that same `parent_id`, ordered by `order_index` (`IS` instead of `=`
    so the top-level, `parent_id IS NULL` case matches correctly). A
    chunk with no previous/next sibling, or no parent, degrades to an
    empty list/`None` rather than an error.
  - **`retrieval/context_builder.py`**: new `expand_chunk_context`
    returns an `ExpandedChunkContext` with `matched` (the actual hit,
    fetched by its own id so it's the full stored chunk, not just an FTS
    snippet) structurally separate from `parent_heading`/`previous`/
    `next` -- a consumer (CLI, MCP) can never mistake surrounding context
    for the match itself, only ordering or a flat list could have implied
    that distinction. Budget (`SearchContextConfig.max_tokens`, reusing
    `documents/tokenization.count_tokens`) is spent in priority order --
    the matched chunk's own text always counts but is never dropped for
    it; then the parent heading; then previous/next siblings nearest-
    to-farthest per side, so the first piece that would overflow is
    dropped along with everything farther from the match on that side,
    keeping what's shown a contiguous window rather than an arbitrary
    subset. Every drop is reported in `truncated`/`truncation_reasons`,
    never silent.
  - **`core/config.py`**: new `search.context` (`SearchContextConfig`):
    `parent_heading` (default `true`), `previous_chunks`/`next_chunks`
    (default `1` each), `max_tokens` (default `1200`). Setting
    `parent_heading: false` and both sibling counts to `0` disables
    expansion outright through the same code path (nothing to fetch),
    rather than needing a caller to special-case "off".
  - **`cli/search.py`**: `--json`/the default snippet-block view now
    attach this chunk's expansion (a `context` key on `--json`'s document
    hits; an `Expanded context:` block with `[heading]`/`[previous]`/
    `[next]`-tagged lines under `Match:` otherwise) -- computed only for
    "json"/"snippets" mode and only when `search.context` isn't fully
    off, so "table"/"files" mode, or an all-zero config, pays nothing
    extra for the per-hit DB lookups.
  - New `tests/unit/test_documents_repo_context.py` (the repository
    lookup: mid-document/first/last chunk, multiple requested siblings
    against a document with fewer, each config knob off, an unknown
    chunk id), `tests/unit/test_context_builder_chunk_expansion.py`
    (`expand_chunk_context`: the same toggles, plus budget trimming --
    a tight budget that keeps only the matched chunk, and a budget sized
    to keep the parent heading and nearest sibling but drop the farther
    one), and a new `tests/integration/test_context_expansion_cli.py`
    indexing a real small multi-paragraph Markdown document end to end
    and confirming the expected neighboring paragraph text actually
    appears in a mid-document hit's expanded context, that expansion is
    absent when every knob is off, and that ranking order is identical
    with expansion on vs. off for the same query.
- Search Quality Improvement Plan, Phase 10: five more document formats
  genuinely convert, index, and are lexically/semantically searchable --
  CSV, ODT, ODS, ODP, EPUB -- each verified end-to-end against real
  extracted content (never an empty-text round-trip), plus opt-in OCR
  support for raw images. Several formats the plan expected to be free
  turned out, on reading the *installed* Docling version's actual backend
  source (not just assuming its tier list), to require a dependency this
  project deliberately avoids -- deferred honestly rather than partially
  wired and claimed as supported.
  - **Genuinely supported, with fixture + indexing + lexical + semantic
    coverage for each**: `DocumentFormat` (`core/models.py`) and
    `docling_adapter.EXTENSION_TO_FORMAT`/`_format_to_input_format`
    (`documents/docling_adapter.py`) grow `CSV`/`ODT`/`ODS`/`ODP`/`EPUB`,
    each converted by one of Docling's own rule-based backends (no
    layout/table-structure model, no LibreOffice) -- `sources/detector.py`
    `DOCUMENT_EXTENSIONS` grows `.ods`/`.odp`/`.epub` (`.csv`/`.odt` were
    already detected, just not converted). New fixtures under
    `tests/fixtures/documents/` (`simple.csv` hand-written like
    `simple.md`/`simple.txt`; `document.odt`/`spreadsheet.ods`/
    `presentation.odp` built with `odfdo` and `sample.epub` hand-
    assembled as a minimal ZIP+XHTML archive, both via new functions in
    `generate_fixtures.py`), new golden normalization tests in
    `tests/unit/test_document_normalizer.py`, a new indexing+FTS
    integration test (`test_phase_10_formats_index_and_are_fts_
    searchable` in `tests/integration/test_document_indexing.py`), and a
    new semantic-search integration test
    (`test_semantic_search_surfaces_phase_10_document_formats` in
    `tests/integration/test_semantic_retrieval.py`, using the existing
    fake-hash-embedder convention -- no real model load needed).
  - **New dependency: `odfdo`** (`pyproject.toml`). Docling's ODT/ODS/ODP
    backend imports it behind a guarded try/except exactly like
    `mailparser`/`python-oxmsg` do for EML/MSG -- but unlike those (and
    unlike RapidOCR for OCR), `odfdo` is genuinely *not* pulled in
    transitively by plain `docling` at this pinned range's current
    resolution, so it's this phase's one explicit new dependency. It
    earns an exception to this project's stated "avoid heavy/optional
    Docling extras" policy: pure Python, its only import is the
    already-required `lxml`, no ML weights, no subprocess, no system
    binary -- the same weight class as the dev-only `python-docx`/
    `python-pptx`/`openpyxl` this project already accepts for DOCX/
    PPTX/XLSX fixture generation, just needed at runtime here (to
    actually convert a project's real `.odt`/`.ods`/`.odp` files, not
    only to build test fixtures).
  - **Raw images, opt-in via new `documents.image_ocr` config field**
    (`core/config.py`, default `False`): `.png`/`.jpg`/`.jpeg`/`.tif`/
    `.tiff` are genuinely convertible -- verified end-to-end against real
    OCR'd text, using the same RapidOCR engine Phase 5 already confirmed
    ships transitively -- but an image has no embedded text layer at
    all, so real extraction always means a full OCR pass with no cheap
    non-OCR path to try first the way PDF has. `sources/detector.py`
    detects these extensions unconditionally; `documents/pipeline.py`
    only actually converts them when `documents.image_ocr` is enabled,
    falling back to the same "detected, no derived content" behavior as
    an unsupported extension otherwise, so an image-heavy source doesn't
    silently make every index run much slower unless a project opts in.
    New fixture `tests/fixtures/documents/sample_ocr.png` (Pillow-
    rendered text, real pixels -- not embedded-as-data), a new
    `docling_pdf`-marked golden test
    (`test_image_ocr_enabled_extracts_real_text_via_ocr` in
    `tests/unit/test_docling_pdf.py` -- reuses that marker rather than
    adding a new one, since OCR is the same "real network + model
    download on first use" shape PDF's own model already needs, just
    from ModelScope instead of Hugging Face), and two new integration
    tests in `tests/integration/test_document_indexing.py`: the default-
    off fallback path (default suite) and the opt-in real-OCR path
    (`docling_pdf`-marked).
  - **Deferred, with the specific reason for each** (`docling_adapter.py`'s
    module docstring has the full detail):
    - **`.rtf`** -- the plan expected this to be free (Tier 1), but
      reading the installed Docling version's actual
      `msword_backend.py` shows `MsWordDocumentBackend.__init__`
      unconditionally shells out to LibreOffice (`soffice --convert-to`)
      for `InputFormat.RTF`, exactly like legacy `.doc`. No pure-Python
      fallback exists. This project's explicit policy is to never add a
      LibreOffice dependency, so `.rtf` stays exactly as before:
      detected, not converted.
    - **Legacy `.doc`/`.ppt`/`.xls`** -- confirmed (not just assumed)
      to have the same mandatory-LibreOffice requirement, reading
      `msword_backend.py`/`mspowerpoint_backend.py`/`msexcel_backend.py`
      directly. Empirically reinforced, not just reasoned about: even in
      a development sandbox where a `soffice` binary happened to be
      present, headless `--convert-to` invocations failed outright on
      every input tried (RTF and plain text alike) -- underlining
      exactly why this project treats LibreOffice as an environment
      dependency to avoid rather than one more format to wire up.
    - **Outlook `.msg`** -- deferred for a different, non-technical
      reason: Docling's `EmailDocumentBackend` and its guarded-import
      deps (`mail-parser`, `python-oxmsg`) are already transitively
      installed, so `.msg` would need zero new code or dependencies.
      But authoring a genuine, valid `.msg` fixture requires a real
      Outlook-produced OLE2/CFBF (MAPI) binary structure -- there is no
      writer library available here and no network access in this
      environment to fetch a real sample, so a hand-rolled fixture risks
      either failing to parse (proving nothing) or silently round-
      tripping empty text, which this plan's acceptance criteria
      explicitly forbids claiming as "supported". Left for a future
      phase with a real sample fixture or a `.msg`-writing dependency.
  - **`indexing/coordinator.py`**: `ProcessorContext` grows `image_ocr:
    bool | None`, threaded from `config.documents.image_ocr` exactly
    like `ocr`/`chunking`/`max_document_pages` already are.

- Search Quality Improvement Plan, Phase 12: version-aware incremental
  reuse -- derived processing (chunks, FTS, embeddings) now keys off a
  composite reuse identity (`content_hash` + `parser_version` +
  `chunker_version` + `embedding_model_id` + `embedding_text_version`),
  not `content_hash` alone, so a chunking/embedding-assembly/parser or
  embedding-model change is never silently left unreflected in already-
  indexed content just because the underlying bytes didn't change.
  Rebuild work is minimized without risking stale search data: a moved/
  renamed file reuses its parsed document, chunks, and embeddings
  outright; a full derivation-logic change rebuilds chunks/FTS/
  embeddings; an embedding-model-only change rebuilds vectors alone,
  leaving `document_sections`/`document_fts` untouched.
  - **New version constants** (all introduced at their current value --
    this phase adds tracking, it does not change any derivation's actual
    behavior): `documents/docling_adapter.py`'s `PARSER_VERSION`
    (deliberately independent of the existing `_CACHE_VERSION`, which
    guards the PDF conversion cache's own row format -- see that
    constant's docstring for why aliasing them would have forced a full
    reindex of every already-indexed document the moment this phase
    shipped), `documents/chunker.py`'s `CHUNKER_VERSION` (chunk
    boundaries/`search_text`) and `EMBEDDING_TEXT_VERSION`
    (`_contextual_text`'s breadcrumb assembly, versioned separately since
    it gates a narrower rebuild), and `indexing/embedding_indexer.py`'s
    `CODE_EMBEDDING_TEXT_VERSION` (the code-entity counterpart of
    `EMBEDDING_TEXT_VERSION`, for `_entity_text`).
  - **New nullable, no-backfill `files` columns** (`storage/schema.py`
    `KNOWLEDGE_DB_V14`, migration 14): `chunker_version`,
    `embedding_model_id`, `embedding_text_version`, alongside the
    already-existing `parser_version` column (Phase 1), which this phase
    starts actually writing meaningfully for the first time. A `NULL`
    stamp (every file indexed before this migration) is never itself
    treated as stale -- only a stamp that is both known and disagrees
    with current code forces a rebuild (`indexing/incremental.
    decide_reprocessing`'s `_stale` helper) -- so shipping this tracking
    infrastructure does not, by itself, force a full reindex of any
    already-indexed project.
  - **`indexing/incremental.py`** grows `VersionStamp`, `ReprocessDecision`
    (`NONE`/`FULL`/`EMBEDDINGS_ONLY`), and `decide_reprocessing`, layered
    on top of the existing content-hash `classify_change`/`find_deleted`.
  - **`indexing/coordinator.py`**: `ProcessorRegistry.register` grows an
    optional `version_provider` callable per `FileKind` (only
    `FileKind.DOCUMENT` registers one, via `documents/pipeline.py`'s new
    `document_version_stamp` -- `FileKind.CODE` deliberately does not,
    since Tree-sitter already reparses a changed code file from scratch
    with nothing left for a chunker-style version axis to catch). A new
    `_reconcile_renames` step, run before `find_deleted`, matches a
    scanned path with no exact existing-path match back to an existing
    file record by content hash (only when the match is unambiguous and
    the detected `FileKind` agrees), reassigning that row's path in place
    (`files_repo.rename`) instead of the delete-then-insert-as-new a
    plain path mismatch previously caused -- the actual mechanism that
    makes a moved/renamed file's chunks/embeddings reusable, distinct
    from (and downstream of) the Phase 1B PDF-conversion-cache reuse a
    moved file already got. A new `_version_reprocess_decision`, applied
    to a content-`UNCHANGED` file, upgrades it to a full reprocess
    (`ReprocessDecision.FULL`) or records it for a narrower rebuild
    (`ReprocessDecision.EMBEDDINGS_ONLY`, via new `IndexRunResult`
    fields `embeddings_stale_code_file_ids`/
    `embeddings_stale_document_file_ids`, kept separate from
    `touched_*_file_ids` so Phase 4's cross-domain linking pass stays
    scoped to genuinely touched files only). `IndexRunResult` also grows
    `moved` (surfaced in `ragpilot index`'s summary line).
  - **`indexing/embedding_indexer.py`**: `embed_touched_files` now unions
    `embeddings_stale_*_file_ids` into its touched-files scope (wired in
    `indexing/runner.py`) and stamps `files.embedding_model_id`/
    `embedding_text_version` right after a file's vectors are genuinely
    (re)computed -- never speculatively, so a skipped/failed embedding
    step (e.g. `EmbeddingModelUnavailableError`) never claims a rebuild
    that didn't happen.
  - **`storage/repositories/files_repo.py`**: `mark_indexed` grows
    optional `parser_version`/`chunker_version` parameters (`COALESCE`d
    against the existing value, so a processor with no version provider
    never clobbers a real stamp with `NULL`); new `update_embedding_version`
    and `rename` functions.
  - New tests: `tests/unit/test_incremental.py` (`decide_reprocessing`
    scenarios), `tests/unit/test_files_repo.py`, `tests/unit/
    test_index_coordinator_rename.py`, extended `tests/unit/
    test_embedding_indexer.py`, and a new end-to-end integration suite
    `tests/integration/test_incremental_reuse.py` proving, through the
    real CLI, that: a path-only change reuses chunks/embeddings
    byte-for-byte; a `chunker_version` bump rebuilds chunks/FTS/
    embeddings *and* the old chunk text is no longer findable while the
    new derivation is; an `embedding_model_id` bump rebuilds vectors only
    (chunks/FTS untouched, old model's vectors gone, new model's vectors
    present and different); and unchanged content with no version change
    rebuilds nothing at all (not even a `files.updated_at` bump).

- CLI performance improvement plan, Phase 1: startup benchmark and
  heavy-import regression test.
  - **Benchmark suite** (new top-level `benchmarks/cli_startup/` package,
    `pytest -m cli_startup_benchmark`): measures real subprocess
    wall-clock startup (cold + warm p50/p95) for the lightweight commands
    that should start almost immediately -- `version`, `--help`,
    `config --help`, `status`, `search --help` -- against warm-start
    budgets in `targets.py`. Same non-blocking pattern as
    `benchmarks/search/`: millisecond numbers are reported, not
    hard-asserted, in CI (a shared/virtualized runner is not real,
    unshared hardware); `python -m benchmarks.cli_startup --strict` is
    the form that actually enforces them, for a real machine.
  - **Architectural regression test**
    (`tests/unit/test_cli_startup_imports.py`, always-on/default suite):
    runs each lightweight command in a fresh subprocess and asserts
    `sys.modules` never picks up Docling/torch/transformers/mcp/openai/
    anthropic/usearch -- the actual hard CI gate against startup
    regressing, since exact timing on a shared runner cannot be. Marked
    `xfail(strict=True)` for now: `ragpilot.cli.main` currently imports
    every CLI submodule eagerly, which transitively loads the full heavy
    stack regardless of command; a follow-up phase removes those eager
    imports and flips this to a plain (passing) assertion.

- CLI performance improvement plan, Phase 2: lazy imports for the three
  chains eagerly reachable from `ragpilot.cli.main` (which imports every
  CLI submodule up front, regardless of which command was invoked) into
  Docling, the `mcp` SDK, and the `openai`/`anthropic` SDKs.
  - `indexing/runner.py`'s `build_processor_registry` now imports
    `documents.pipeline.document_processor` inside the function (still
    gated behind `config.documents.enabled`) instead of at module level --
    severs the one path (`cli/index.py`, `cli/rebuild.py` via
    `ops/rebuild.py`, `cli/watch.py`/`cli/daemon.py` via
    `service/daemon.py`) that pulled Docling (and, through it, torch) into
    every one of those commands' imports.
  - `documents/docling_adapter.py` no longer imports Docling/docling_core
    at module level at all: `InputFormat`/`ConversionStatus`/
    `PdfPipelineOptions`/`DocumentConverter`/`PdfFormatOption`/
    `DocumentStream` are now imported inside the functions that actually
    construct or use them (`_get_converter`, `_get_md_converter`,
    `_run_conversion`, `_reparse_markdown`); `DoclingDocument` is
    `TYPE_CHECKING`-only (safe under this module's existing
    `from __future__ import annotations`). The `InputFormat`-keyed format
    map is now built lazily and cached (`_format_to_input_format`) rather
    than at import time.
  - `cli/serve.py` now imports `mcp.server.run_stdio` inside `serve()`,
    right before calling it, instead of at module level -- the `mcp` SDK
    now only loads for `ragpilot serve --mcp`.
  - `ai/factory.py`'s `create_provider` now imports each provider module
    (`ai/openai.py`, `ai/anthropic.py`, `ai/openai_compatible.py`,
    `ai/ollama.py`) inside its own branch instead of importing all four at
    module level -- `ragpilot ask` now loads only the SDK for whichever
    `ai.provider` is actually configured, not every provider's SDK.
  - Net effect, measured with `python -m benchmarks.cli_startup` (Phase
    1's benchmark): `version`/`--help`/`config --help`/`status`/
    `search --help` each dropped from ~9s to roughly 500-600ms subprocess
    wall-clock on shared/virtualized hardware -- still short of the
    aggressive targets in `benchmarks/cli_startup/targets.py` (which
    assume real, unshared developer hardware, per that module's own
    docstring) but a ~15-18x improvement. `tests/unit/test_cli_startup_imports.py`
    (Phase 1's `xfail(strict=True)` regression test) is now a plain,
    passing assertion -- the fix that test was written against. Also
    fixes a latent bug in that test's own subprocess-output parsing (masked
    by `xfail` until now): it naively split the entire captured stdout on
    commas, which broke on `--help`'s own usage text (real commas in
    argument descriptions); now reads a single marker-prefixed line
    instead.

- CLI performance improvement plan, Phase 3: manual update discovery --
  `ragpilot update`/`update check`/`update status`/`update install`.
  - New top-level `update/` package: `versioning.py` (installed version +
    minimal MAJOR.MINOR.PATCH parsing/comparison -- full SemVer 2.0
    pre-release/build-metadata precedence is out of scope, since this
    plan's own release process (Phase 6) only ever produces plain
    `MAJOR.MINOR.PATCH` tags), `checker.py` (queries GitHub's releases API
    for `gzarog/Ragpilotv2` -- hardcoded, not configurable, and HTTPS
    only -- and rejects any release whose tag is not a valid semantic
    version), `cache.py` (`<RAGPILOT_HOME>/update.json` read/write, same
    write-then-rename durability pattern as `service/health.py`'s daemon
    health snapshot), `models.py`, and `installer.py` (currently a stub:
    installation-method detection and the real upgrade path are a later
    phase -- see below).
  - New `cli/update.py`: `ragpilot update check` queries GitHub and
    prints/writes the result (`Installed: X` / `Latest: Y` / "Update
    available. Run: ragpilot update install" or "RAGpilot X is up to
    date."); `ragpilot update status` reads the cache only and never
    talks to GitHub; bare `ragpilot update` behaves like `update check`.
    `ragpilot update install` exists but clearly reports it isn't
    implemented yet, with the manual `curl | sh` / `irm | iex` / `pip
    install` upgrade commands as a stand-in -- a later phase replaces
    this stub with the real installation-method-aware upgrade path
    (Definition of Done's "provides or performs the correct upgrade
    path" isn't met yet by design). Distinct from the pre-existing
    `ragpilot upgrade` (database schema migrations), which is unchanged.
  - No command performs a synchronous GitHub request except the explicit
    `ragpilot update check` (and, transitively, bare `ragpilot update`);
    `update status` and every other command only ever read the local
    cache. Automatic/background checking, and the startup notification
    that reads this same cache, are a later phase.
  - Validated against the real repository during development:
    `ragpilot update check` successfully reached GitHub through this
    environment's proxy and correctly *rejected* `gzarog/Ragpilotv2`'s
    current release (tagged `ragpilot_0_1_0`, not `v*.*.*`) as an invalid
    version rather than crashing or misreporting it -- exactly the
    Security Requirements' "accept valid semantic versions only"
    behavior. That tag predates this plan's tagging convention and is
    expected to be superseded once Phase 6 lands.

- CLI performance improvement plan, Phase 4: cached automatic update
  notifications -- no command makes a synchronous GitHub request.
  - New `updates:` config section (`enabled`, `check_interval_hours`,
    `notify`, `channel` -- `RAGPILOT_UPDATES__*` env overrides). `channel`
    is reserved for a future non-stable release channel; accepted and
    stored, not yet acted on, since this repository only publishes one
    channel today.
  - `update/background.py`: `AppContext.bootstrap()` -- the one path
    every "normal" command shares, and specifically *not* `version`/
    `--help`/`config`/`update ...`, none of which call it -- now decides,
    from the local cache's age (`updates.check_interval_hours`, default
    24h), whether to spawn a fully detached `python -m
    ragpilot.update.background <home>` process that makes the one real
    GitHub request and writes the cache, then exits. The calling command
    never waits on it; a failed spawn (no fork permission, etc.) is
    swallowed. A newer discovered version resets the "already notified"
    marker; rediscovering the same one on the next scheduled check does
    not.
  - `update/notifier.py`: `AppContext.close()` -- run at the end of every
    normal command, after its own output -- reads that same cache (never
    GitHub) and prints "A newer RAGpilot version is available: X → Y" at
    most once per version, always to stderr so it never lands inside a
    `--json` payload or (`ragpilot serve --mcp`) the MCP stdio transport's
    JSON-RPC channel.
  - Both hooks wrapped in `contextlib.suppress` at the `AppContext` call
    site: a bug in either must never break a command's own execution or
    its exit code.
  - Test-suite safety net: a new autouse fixture
    (`tests/conftest.py::_disable_background_update_checks`) sets
    `RAGPILOT_UPDATES__ENABLED=false` for every test by default --
    without it, every existing test calling `AppContext.bootstrap()`
    (hundreds of them) would have spawned a real detached subprocess
    making a real GitHub request on every run.
  - Manually verified end-to-end against the real repository: with a
    simulated stale-but-populated cache, `ragpilot status` printed the
    notification once (after its own table output, on stderr) and
    correctly did not repeat it on the next invocation; with no cache,
    the command still completed in well under a second while the
    (real, unmocked) background check ran and -- as in Phase 3's own
    validation -- found this repository's current release tag invalid
    and silently wrote nothing, exactly the designed offline/failure
    behavior.

- CLI performance improvement plan, Phase 5: `ragpilot update install`,
  installation-method detection, and migration/health-check integration.
  - `install.sh`/`install.ps1` now write `<RAGPILOT_HOME>/install_info.json`
    (`install_method`, `repository`, `install_dir`, `venv_dir`, `bin_dir`)
    after a successful install-script install -- the authoritative signal
    `update/installer.py`'s `detect_install_method` checks before falling
    back to runtime heuristics: a PEP 610 `direct_url.json` check for an
    editable/dev install (`pip install -e .`), then a pipx-shaped venv
    path, then "pip" for any other installed distribution, else "unknown".
    CI's install-script job now also asserts that file gets written.
  - `update/installer.py`'s `install_latest` runs this plan's full
    sequence: check the latest release, confirm it's actually newer
    (a no-op "already up to date" return otherwise), detect the install
    method, run that method's upgrade command (`curl | sh`/`irm | iex`
    re-run with `RAGPILOT_REF` set to the validated release tag for
    install-script; `pip install --upgrade "git+...@<tag>"`; `pipx
    install --force "git+...@<tag>"`), then -- via `sys.executable`
    again, so this runs the *newly* upgraded code rather than the old
    process's already-imported modules -- `ragpilot upgrade` (schema
    migrations) and `ragpilot doctor` (health check), reusing those
    existing commands rather than duplicating their logic. An editable/
    dev install or an undetectable method refuses with clear manual
    instructions instead of guessing. The release tag only ever reaches a
    subprocess via an environment variable or as one non-shell-interpreted
    argv element, never interpolated into a shell string -- defense in
    depth on top of `checker.py`'s existing tag-format validation.
    `ragpilot update install` replaces Phase 3's "not implemented yet"
    stub; `ragpilot upgrade` (schema migrations) is unchanged.
  - The whole sequence is built around an injectable command-runner seam
    (`tests/unit/test_update_installer.py`), so its tests never spawn a
    real `curl`/`pip`/`pipx`/`powershell` process; manually confirmed
    against this real sandbox's own editable dev install that
    `detect_install_method` correctly reports `"editable"`.

- CLI performance improvement plan, Phase 6: one source of truth for the
  version, and an automatic patch release on every successful merge to
  `main`.
  - **Tag-derived versioning**: `pyproject.toml` now declares
    `dynamic = ["version"]` with `[tool.hatch.version] source = "vcs"`
    (via the `hatch-vcs` build backend) instead of a version duplicated
    by hand in both `pyproject.toml` and `src/ragpilot/__init__.py`. An
    immutable git tag (`v0.1.8`, ...) is now the only source of truth;
    `[tool.hatch.build.hooks.vcs]` writes the resolved version to the
    gitignored `src/ragpilot/_version.py` at build/install time, and
    `__init__.py` imports `__version__` from it (falling back to
    `"0+unknown"` for a raw, never-installed source checkout). An
    editable/unreleased build gets a PEP 440 dev version instead (e.g.
    `"0.1.dev39+gd6cd42e.d20260911"`) -- `update/versioning.py`'s
    `is_newer` now parses the installed side with `packaging.version`
    (already a transitive dependency of the packaging toolchain itself;
    now declared explicitly) so comparing against a dev build never
    mistakes "no tag yet" for "no update available"; the untrusted,
    externally-sourced side (a GitHub release tag) stays held to the
    existing strict `MAJOR.MINOR.PATCH`-only validation.
  - **`.github/workflows/main-release.yml`** (new): triggered by
    `workflow_run` on `ci.yml`'s "CI" workflow completing, filtered to a
    run whose head branch is `main` (never a pull request's own CI run)
    and whose conclusion is `success` (so a failing required check on
    `main` blocks the release exactly like it should) -- reads the
    latest `v*.*.*` tag, bumps its patch component (defaulting to
    `v0.1.0` when no tag exists yet, as is currently the case for this
    repository), and pushes the new tag. `concurrency: group:
    main-release` serializes back-to-back merges so two close-together
    releases can't both compute the same next version from a stale read.
    No commit is ever pushed back to `main` to record the new version --
    the tag itself *is* the record (this plan's own stated reason to
    prefer tag-derived versions: it can't recursively re-trigger this
    same workflow the way editing `pyproject.toml`/`__init__.py` back
    into `main` could).
  - **`.github/workflows/release.yml`** now also accepts `workflow_call`
    (alongside its existing `on: push: tags: "v*.*.*"`, unchanged for a
    human/automation with real push access pushing a tag directly) --
    `main-release.yml` calls it explicitly right after pushing the new
    tag, rather than depending on that push to trigger it the normal way:
    a push authenticated with the default `GITHUB_TOKEN` deliberately
    never triggers another workflow's own `on: push` (GitHub's built-in
    anti-recursion rule), so relying on that would have silently built
    nothing. `tag_name`/the checkout `ref` are now `inputs.tag ||
    github.ref_name` throughout, since `GITHUB_REF` for the
    `workflow_call` path is `main`'s branch ref, not the tag.
  - `ci.yml`'s required `lint-and-typecheck`/`test` jobs and
    `release.yml`'s build job now check out full history (`fetch-depth:
    0`) instead of the default shallow clone -- hatch-vcs needs every
    tag reachable to compute a meaningful version, which a depth-1 clone
    can't provide.
  - **Validated locally, end to end**: built a real sdist/wheel via
    `python -m build` against this branch's actual (dirty, no-tag)
    working tree -- succeeded, producing a PEP 440 dev version. Then,
    with a clean working tree and a local `v0.1.0` tag at HEAD, rebuilt
    and got exactly `ragpilot-0.1.0` (no dev suffix) -- confirming the
    exact scenario `release.yml`'s build job will see for a real tagged
    release. The tag-bump shell logic in `main-release.yml` was verified
    against several tag sets (none, sequential, double-digit components
    like `v0.1.9`→`v0.1.10`, mixed major versions) via `sort -V`, which
    orders them numerically rather than lexicographically.
  - **Two real bugs this surfaced in CI, both in the hatch-vcs config,
    fixed in this same change**: (1) `pip install`-ing from a plain
    source tarball/zipball (exactly what `install.sh`/`install.ps1`
    download) has no `.git` directory at all, and setuptools-scm (which
    hatch-vcs wraps) hard-failed the entire install rather than falling
    back -- fixed with `fallback-version = "0.0.0"`. (2) this
    repository's actual first release predates this plan's tagging
    convention (tagged `ragpilot_0_1_0`, not `v0.1.0`) and, once
    `fetch-depth: 0` made every tag reachable, setuptools-scm's tag
    selection (`git describe`, unconditional nearest-tag, parsed only
    *after* selection) picked that one and hard-failed trying to parse
    it as a version -- `tag-pattern`/`tag_regex` only affects parsing
    *after* selection, so it can't prevent this; fixed with a
    `[tool.hatch.version.raw-options]` `git_describe_command` override
    adding `--match 'v*.*.*'`, which excludes it from ever being
    selected as a candidate in the first place. Both reproduced and
    fixed locally against this repository's actual tag before pushing:
    a real archive with no `.git` now builds `ragpilot-0.0.0`; a `.git`
    checkout with only the `ragpilot_0_1_0` tag reachable now builds a
    `0.0.1.dev<N>+g<sha>` dev version instead of failing outright; a
    clean `v0.1.0` tag at HEAD still builds exactly `ragpilot-0.1.0`.

- `ragpilot uninstall [--keep-data] [--yes] [--json]`: removes the
  installed application and, by default, all of its data.
  - Reuses `update/installer.py`'s `detect_install_method` (the same
    "how was this installed" question `ragpilot update install` already
    answers) to dispatch application removal: `pip uninstall -y
    ragpilot`, `pipx uninstall ragpilot`, or -- for an install-script
    install -- deleting exactly `venv_dir` and `install_dir/app` plus the
    one launcher file in `bin_dir`, read from `install_info.json`. Never
    the whole `install_dir` (it can share a parent directory with
    `RAGPILOT_HOME` by default) and never anything else that happens to
    live alongside the launcher in the shared `bin_dir`. An editable/dev
    install or an undetectable method is never auto-removed -- clear
    manual instructions are reported instead, independent of whether data
    was purged.
  - Data purge (default; `--keep-data` skips it): stops a running daemon
    first, then deletes `RAGPILOT_HOME` entirely -- the same pre-delete
    safety step `ops/restore.py` already took before swapping in a
    backup, now shared via a new `service/pid.py` helper
    (`stop_and_wait`) rather than duplicated a second time.
  - Prompts for confirmation (listing exactly what will be deleted)
    before touching anything, unless `--yes`/`-y` is given.
  - On Windows, an install-script uninstall's file removal is a
    short-lived, fully detached process that waits for this process to
    exit first (the same reason `ragpilot update install`'s upgrade step
    launches a separate process) -- deleting files this running
    interpreter has open can fail outright on Windows, unlike POSIX,
    where a direct, synchronous removal is reliable.
  - Manually verified end to end in this sandbox: declining the prompt
    leaves everything untouched; `--yes` purges data and correctly
    reports manual removal instructions for this sandbox's own editable
    install; a real running daemon is stopped (confirmed via `ps`) before
    its data directory is deleted; `--keep-data` leaves `RAGPILOT_HOME`
    in place.

- Fixed a real-world Windows bug in `ragpilot update install`'s
  install-script path: it re-runs `install.ps1` from inside the
  currently-running `venv\Scripts\ragpilot.exe`, which rebuilt that same
  venv in place -- deleting or overwriting its own running exe file, which
  Windows refuses (a sharing violation pip surfaced as `ERROR: Could not
  install packages due to an OSError: [WinError 32] ... being used by
  another process`). `install.ps1` now renames the existing venv out of
  the way first (a directory rename succeeds even with an open file
  inside it, unlike deleting/overwriting that file directly) and builds
  the new one fresh, directly at the real venv path -- not at a temporary
  path swapped in afterward, since pip's own generated console-script
  launchers (`ragpilot.exe` included) embed the venv's exact interpreter
  path at install time and break if the venv is relocated post-install
  (this was tried first and caught by the new regression test below,
  which is exactly what it's for). A rename-away left half-done because
  the old venv is still in use is cleaned up automatically at the start of
  the next install/upgrade run, once it's no longer locked. install.sh
  (POSIX) is unaffected -- replacing an open file works there already.
  Covered by a new Windows CI regression test that holds an exclusive
  read lock on the installed `ragpilot.exe` (simulating a running process)
  across a second `install.ps1` run and confirms both that run and the
  resulting CLI still work.

- Fixed `ragpilot version`/`__version__` always reporting `0.0.0` for
  every install-script install: `install.sh`/`install.ps1` download a
  branch or tag *archive* (zip/tarball) from GitHub, which never includes
  a `.git` directory, so hatch-vcs can never derive a real version from
  it and always falls back to the hardcoded `0.0.0` placeholder (see the
  entry above this one). Both scripts now set
  `SETUPTOOLS_SCM_PRETEND_VERSION` before the real `pip install`, resolved
  from `$Ref`/`$REF`: an explicit release tag (the shape
  `update/installer.py`'s upgrade path always passes) is used directly;
  the default `main` instead queries GitHub for the latest actual
  release, since `main` itself isn't a version; any other custom/branch
  ref is left unresolved, reporting the honest `0.0.0` rather than an
  unrelated release's version. Verified end to end against a real
  `.git`-less copy of this repository: unset, the build is `ragpilot-0.0.0`
  (confirming the bug); with the env var set, both the built wheel's
  version metadata and the installed `ragpilot.__version__` reflect it
  exactly.

- Fixed a second real-world Windows bug in `ragpilot uninstall`,
  reported against a live install: it purged `RAGPILOT_HOME` *before*
  removing the application, but install.sh/install.ps1's default layout
  nests the venv *inside* `RAGPILOT_HOME` (they share the same default
  root) -- purging data first could delete the very interpreter the
  `pip uninstall`/`pipx uninstall` step (run via `sys.executable`) then
  needed, breaking it outright (`failed to locate pyvenv.cfg: The system
  cannot find the file specified`, followed by a broken `rich` import).
  `cli/uninstall.py` now removes the application first and purges data
  second -- application removal never touches `RAGPILOT_HOME`, so the
  reverse order has no equivalent risk. Covered by a new test asserting
  the call order directly.

- `ragpilot search`: document hits now render as match-centered snippet
  blocks by default (a real usability gap -- there was previously no way
  to see *why* a document matched without a separate `--json` round
  trip and manual truncated-text guesswork):
  ```
  PDF: 1177646_0076000_1.pdf
  Page: 2
  Match:
  HDL Cholesterol .......... 51 mg/dL
  ```
  - The snippet itself is real: SQLite FTS5's own `snippet()` function
    (`documents_repo.search_fts_projection`), auto-picking whichever
    indexed column (`heading_text`/`body`/`doc_title`) actually matched
    and bounding the excerpt to `search.output.snippet_max_tokens`
    (default 32, clamped to FTS5's own 1-64 limit) -- replacing the
    previous naive `(body or heading)[:280]` character slice, which
    would silently return unrelated leading text instead of the actual
    match for anything past the first ~280 characters of a paragraph.
    This also upgrades every other consumer of `SearchResult.snippet`
    (`--json`, the MCP `ragpilot_search` tool, `--hybrid`), not just the
    new block rendering.
  - Page number (PDF/DOCX/PPTX, `document_sections.page_start`/
    `page_end`, now joined into the FTS query and `SearchResult.location`
    for the first time) or heading path (Markdown/HTML/plain text, which
    have no page concept) is shown alongside the excerpt, whichever the
    format actually has.
  - `--snippets` forces this mode explicitly; `--table` reverts to the
    single title/path/tier table every result kind shared before this
    existed. Code/entity hits are unaffected either way -- they always
    render via that same plain table row, confirmed as out of scope for
    this change (a source-code snippet is a different feature).
  - The default mode, and what one document hit falls back to when it
    has no real match snippet (an exact-title hit with no FTS row),
    are both driven by the same new `search.output.fallback` config
    list (`SearchOutputConfig`, `["snippets", "json", "files"]` out of
    the box) -- "configurable fallback" has one meaning, not two.
    `ragpilot config set search.output.fallback json,files` (or hand-edit
    `config.yaml`); `ragpilot config set` learned to coerce a
    comma-separated value into a list for this (the only list-typed
    config field so far).
  - Fixed in the same change: `cli/_common.py`'s `print_json` was calling
    `json.dumps` without `ensure_ascii=False`, so any non-ASCII indexed
    content (a real report against a live install: Greek lab-report
    text) came out as unreadable `\uXXXX` escapes in every `--json`
    command's output, not just `search` -- the underlying extracted text
    was always correct; only its terminal rendering was broken.

- Fixed `ragpilot index` crashing with `Error: UNIQUE constraint failed:
  files.source_id, files.path` on a brand-new source's very first index
  run -- reported live on Windows against a real repo layout. Two
  independent gaps combined: (1) `sources/scanner.py`'s `scan()` had no
  intra-run deduplication, so the same real file could be yielded twice
  in one pass -- on Windows, an NTFS junction (`mklink /J`, common in
  repo-sync/build-artifact layouts) is invisible to both
  `Path.is_symlink()` and `os.walk`'s own `followlinks` (neither
  recognizes `IO_REPARSE_TAG_MOUNT_POINT` the way they do a real
  symlink), so a junction looping back to an already-reached directory
  still gets walked into even with `follow_symlinks=False` (the
  default); (2) `indexing/coordinator.py`'s `IndexCoordinator.run()`
  checked each scanned path against `existing_by_path`, a dict snapshot
  taken once *before* the per-file loop and never updated as new files
  were inserted within that same run, so a duplicate scanned path was
  misclassified as "new" a second time and crashed on the second
  insert. Fixed at both layers: `scan()` now tracks resolved
  directories and files it has already yielded in this pass and skips a
  repeat (pruning a directory-level duplicate before `os.walk` ever
  descends into it a second time, which also bounds what would
  otherwise be unbounded recursion for a junction looping back to one
  of its own ancestors); `IndexCoordinator.run()` now keeps
  `existing_by_path` in sync as it inserts, so a duplicate scanned path
  from *any* source is recognized as unchanged instead of crashing --
  independent defense-in-depth, not reliant on the scanner fix alone.
  Reproduced and verified with a dedicated regression test that
  disables the coordinator's own scanner-level protection and confirms
  the exact reported `IntegrityError` without the coordinator fix, and
  a clean, correctly-deduplicated run with it.

- Fixed `ragpilot daemon start` popping open a second, visible console
  window on Windows instead of returning silently to the caller's own
  terminal -- reported live. `cli/daemon.py`'s `_spawn` used
  `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` for the detached
  background process; `DETACHED_PROCESS` alone doesn't reliably suppress
  a console window for a console-subsystem child (`python.exe` is one)
  in practice, despite Microsoft's docs describing it as "no console
  handle set". Added `CREATE_NO_WINDOW`, the flag whose specific job is
  suppressing window creation for a console-subsystem process. Covered
  by a new Windows-only regression test that starts the daemon for real
  and checks the Win32 window list directly (`EnumWindows` via
  `ctypes`, the same direct-WinAPI-access pattern as `service/pid.py`)
  for any visible window owned by the spawned PID, rather than trusting
  the spawn flags alone -- there is no other way to catch this class of
  bug in CI, since a headless test run has no other visible trace of a
  window actually appearing.

- `ragpilot install-agent` learned `--client NAME` (repeatable, or
  `--client all`) to automatically register RAGpilot with a real MCP
  client's own config file, not just print/write a generic snippet to a
  path the user names. Four clients, each verified against its current
  vendor docs rather than assumed from memory (schemas differ more than
  expected -- VS Code's top-level key is `servers`, not `mcpServers`;
  Cursor's server entries have no `type` field; Codex uses TOML, not
  JSON):
  - `claude-code`: project-scope `.mcp.json` (cwd), `{"mcpServers": {...}}`.
  - `cursor`: project-scope `.cursor/mcp.json` (cwd), same shape, no `type`.
  - `vscode`: workspace `.vscode/mcp.json` (cwd), `{"servers": {...}}`.
  - `codex`: user-level `~/.codex/config.toml` (`.codex/config.toml`
    project-scope exists too, but only takes effect once Codex has
    separately marked that project "trusted", so the user-level file --
    which works unconditionally -- is what this targets).

  Every path is a hardcoded, documented location for that specific
  client (nothing is discovered by scanning the filesystem), and only
  the single `ragpilot` entry within that file is ever added or updated
  -- every other server/setting already in the file is preserved
  untouched, verified for both JSON (deep merge) and TOML (`tomllib` to
  read/detect, since the standard library has no TOML writer; a new
  entry is appended as raw text rather than risking a full-document
  rewrite that could drop comments/formatting elsewhere in a file this
  command didn't create). A JSON client's differing pre-existing
  `ragpilot` entry is corrected in place (a single dict-key replace is
  always structurally safe); Codex's TOML equivalent is left alone and
  reported instead, since appending a second `[mcp_servers.ragpilot]`
  table would be invalid TOML and corrupt the file. Re-running is always
  safe: a matching entry is reported as already configured, not
  duplicated. `--client` and `--write` are mutually exclusive.

- Search Quality Improvement Plan, Phase 11: an optional final neural
  reranking pass over `retrieval/reranker.py`'s already RRF-fused hybrid
  tier -- **explicitly OPTIONAL in the plan, shipped `enabled: false`
  by default, and staying that way after this change** (promotion to
  default-on is a later decision the plan reserves for itself, not this
  one -- see the measured numbers below for why).
  - **New `retrieval/neural_reranker.py`**: loads `cross-encoder/ms-marco-
    MiniLM-L-6-v2` through plain `transformers`
    (`AutoModelForSequenceClassification`, one relevance logit per
    `(query, passage)` pair) rather than adding a new dependency --
    exactly the same "direct `transformers` call, no `sentence-
    transformers` package" reasoning `retrieval/embedder.py` already
    documents, reused for the same underlying `torch`/`transformers`
    stack. `cross-encoder/ms-marco-MiniLM-L-6-v2` is the standard,
    Apache-2.0-licensed, ~90 MB 6-layer MiniLM cross-encoder trained for
    exactly this task (MS MARCO passage relevance ranking). The model
    handle is loaded at most once per process and cached (`_handle`,
    module-level, lock-guarded -- the same pattern `embedder.py` uses),
    and `score_pairs` batches every candidate in one call per
    `_BATCH_SIZE` (16) rather than one model call per candidate.
  - **`rerank_hits(query, hits, *, top_n, score_batch=None)`**: reorders
    only `hits[:top_n]` by cross-encoder score descending, leaving
    `hits[top_n:]` untouched in its existing RRF order -- never applied
    to the whole candidate pool. Falls back to the input list unchanged
    (logging, never raising) whenever the model can't be loaded
    (`NeuralRerankerUnavailableError` -- missing `transformers`/`torch`,
    no network and no cached weights, a corrupt cache, or any other HF
    Hub/local-load failure) or the scorer returns a mismatched count --
    an optional reranking pass can never turn into a hard search
    failure. `score_batch` defaults to the real `score_pairs`, resolved
    dynamically inside the call (not as a bound default argument) so a
    test's monkeypatch of `neural_reranker.score_pairs` reaches every
    caller that omits it, `cli/search.py` included.
  - **`core/config.py`**: new `search.reranker` (`SearchRerankerConfig`):
    `enabled` (default `false`), `top_n` (default `20`, must be `>= 1`).
  - **`cli/search.py`**: within the already-additive `--hybrid` view
    only (never the default, non-`--hybrid` search path) -- when
    `search.reranker.enabled` is `false` (the default), the exact same
    `reranker.rerank(candidates, limit=limit)` call as before Phase 11
    runs, byte-identical output, and `retrieval/neural_reranker.py` is
    never even imported-from-use (no model load, no added latency).
    When enabled, RRF fusion is asked for `max(limit, top_n)` hits so
    the neural pass has its full configured pool to rescore, then the
    combined list is sliced back down to `limit` -- matching the plan's
    own flow (`FTS top 50 + HNSW top 50 -> RRF -> top 20 -> optional
    neural reranker -> top 10`). A new `neural_rerank` stage timing is
    added to `--explain`'s output only in that same enabled path.
  - **Tests**: `tests/unit/test_neural_reranker.py` covers config-free
    logic entirely with a stub `score_batch` in the default suite --
    no-op cases (`top_n<=0`, fewer than two hits), batching (only the
    top-N prefix is ever sent to the scorer), ordering (stable for tied
    scores), query/snippet-vs-title text selection, and both graceful-
    fallback paths (`NeuralRerankerUnavailableError`, a mismatched score
    count) -- plus two `@pytest.mark.reranker_model`-marked tests
    (real model, excluded from the default run) confirming it actually
    ranks a relevant passage above an irrelevant one.
    `tests/integration/test_semantic_retrieval.py` gains CLI-level
    coverage: `search.reranker.enabled=false` (the default) leaves
    `--hybrid` output unaffected even when a monkeypatched
    `neural_reranker.score_pairs` would visibly reorder it if it ever
    ran; enabled, a stub reverser visibly reorders `--hybrid`'s output
    and the `neural_rerank` stage appears under `--explain`; a second,
    unavailable stub falls back to the exact pre-Phase-11 RRF order.
  - **New `reranker_model` pytest marker** (`pyproject.toml`'s
    `addopts`/`markers`, `CONTRIBUTING.md`, a new non-blocking
    `reranker-model-tests` CI job mirroring `embedding-model-tests`):
    kept separate from the existing `embedding_model` marker since the
    two load independent models behind independent config flags.
  - **Measured, not assumed, against this project's own promotion
    gate** (`>= 5% MRR improvement AND an acceptable warm p95 latency
    increase`): `benchmarks/search_quality/evaluator.py`'s existing
    golden-query harness is lexical-only by its own documented design
    (confirmed in Phase 8's entry above) and never exercises `merger.py`/
    `reranker.py`, so it cannot see this phase's effect at all. New
    `benchmarks/search_quality/reranker_evaluation.py`
    (`python -m benchmarks.search_quality.reranker_evaluation`) instead
    runs the full 72-query golden set through the real hybrid pipeline
    (real `sentence-transformers/all-MiniLM-L6-v2` embeddings + real
    `cross-encoder/ms-marco-MiniLM-L-6-v2` reranking, both actually
    downloaded and run in this sandbox -- network and Hugging Face Hub
    access were both available here) twice: once through RRF fusion
    alone, once with the neural pass applied on top of the identical
    RRF-fused candidate pool, and reports MRR (`benchmarks/search/
    quality.py`'s `reciprocal_rank`) and the neural pass's own warm
    (post-model-load) stage latency for both arms. Result, committed as
    `benchmarks/search_quality/reranker_evaluation.json`: MRR
    0.9028 -> 0.9306 (**+3.08%, below the 5% gate**), warm stage latency
    p50 22.5 ms / p95 69.7 ms per query. **Conclusion: on this fixture,
    Phase 11 does not clear its own promotion bar, so `enabled: false`
    is not just this PR's default -- it is the honest, currently-correct
    answer.** This is a genuine measurement on a small (72-query, 6-file)
    fixture, not a claim about every corpus; a larger/harder golden set
    could plausibly show a different delta, which is exactly why the
    plan reserves default-on as a separate, later decision rather than
    deciding it here.

- Search Quality Improvement Plan, Phase 14 (final): full-plan benchmark,
  release-gate scoring, and the canonical baseline regeneration this phase
  is specifically meant to produce -- see
  `benchmarks/search_quality/PHASE_14_FINAL_REPORT.md` for the complete
  comparison table, per-gate numbers, and a Phase 0-13 recap.
  - **True Phase 0 baseline recovered from git history**
    (`git show ce7ecbe:benchmarks/search_quality/baseline_report.json`) --
    the committed `baseline_report.json`/`.md` had stayed at Phase 6's
    snapshot since Phase 6 landed (Phases 7-12 confirmed quality-neutral
    against it), which is *not* the plan's actual Phase 0 starting point.
    Both are now recorded side by side in the new final report so later
    readers don't have to dig through git history to find Phase 0's real
    numbers again.
  - **Release gates, measured**: overall Recall@1 +10.34%, MRR +5.69%
    (target +8%, **short**), NDCG@10 +3.68%; `table_question` reached a
    perfect 1.0 across every metric; every exact-match category
    (`exact_title_lookup`/`exact_heading_lookup`/`exact_symbol_lookup`/
    `file_path_lookup`) stayed byte-identical -- no regression.
    `semantic_document` Recall@5 was already at its ceiling (1.0) in the
    Phase 0 baseline, so the plan's "+10%" gate has no numerical headroom
    to satisfy on this fixture (reported as a numeric FAIL with that
    explanation, not rounded up). Warm hybrid search and re-index-vs-
    full-index both PASS with real before/after numbers, including a new
    medium-scale (50k-subject) direct Phase-0-vs-Final comparison run for
    this phase specifically (`multi_keyword` lexical: 127.8 ms -> 7.7 ms
    p50, a ~16x win from Phase 6/7's tiered lexical plan + weighted BM25;
    `single_keyword`/`hybrid` already missed their strict targets at
    Phase 0 -- a pre-existing, not newly introduced, characteristic of
    this shared/virtualized benchmark hardware).
  - **No tuning constants changed**: `RRF_K=60` (Phase 8), the BM25
    heading/body/title column weights `(5.0, 1.0, 8.0)` (Phase 7),
    chunking's `max_tokens=350`/`overlap_tokens=40` (Phase 2), and the
    reranker's `enabled=false` default (Phase 11) were all reviewed and
    confirmed still measurement-justified as-is; every phase already
    benchmarked its own change against its predecessor before landing, and
    the two gate shortfalls above trace to this fixture's fixed, partly
    ceiling-limited 72-query set rather than to any single mistunable
    constant, so no constant was adjusted just to move an aggregate number.
  - **Canonical `benchmarks/search_quality/baseline_report.json`/`.md`
    regenerated** against this phase's own HEAD (superseding the Phase-6
    snapshot that had been committed since Phase 6) -- quality numbers came
    back byte-identical to the superseded snapshot (re-confirming Phases
    7-12's quality-neutrality claim, now verified directly against Phase 12
    HEAD rather than taken on faith); only latency/size numbers, which
    carry expected run-to-run hardware noise, changed.
  - **Scanned-PDF/OCR retrieval coverage** (Phase 5's target) and a
    PDF-specific re-index measurement (the release gate's literal wording)
    are both noted as genuine benchmark-fixture measurement gaps rather
    than fabricated numbers -- the fixture has no scanned-PDF/image golden
    query and no benchmark isolates PDF re-indexing from the rest of the
    mixed fixture project.
