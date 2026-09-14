# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
