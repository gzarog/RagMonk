# ADR 0022: CLI core workflow (RUST-12, slice 1)

Status: accepted

## Decisions (user)

1. RUST-12 ships in three slices:
   1. Core workflow: `init`, `source …`, `index`, `status`, `docs`,
      `watch` (this slice).
   2. Query commands: `search` (all output modes), `symbol`, `callers`,
      `callees`, `references`, `impact`, `explore`, `link`.
   3. Ops: `doctor`/`health`, `backup`/`restore`, `rebuild`, `upgrade`,
      `uninstall`, `vectors`.
2. `--json` payloads must match the reference exactly. Text output keeps
   the reference's content, order and wording, but as plain text, without
   Rich colors, panels or box tables. Text that the compat manifest gates
   matches byte for byte.
3. Out of RUST-12: `serve` (MCP, RUST-13), `ui`, `ai`/`ask`/
   `install-agent`, and `update`.

## Commands

- **`init`.**
  - Writes the default config, or validates an existing one.
  - Creates and migrates the V2 control plane.
  - Prints the reference's messages.
  - `--storage-mode server` validates the cluster before writing anything:
    - it refuses URLs that contain credentials;
    - it then connects through `ServerBackend::connect`, which checks
      reachability, authentication and engine identity.
  - Interactive prompting is not ported.
- **`source add`.**
  - Checks paths the same way the reference does ("does not exist", "not
    a directory").
  - Uses the same `src_<sha256[:10]>` id.
  - Adding the same path twice returns the existing source.
- **`source list` and `source info`.** `list` prints the reference's
  columns. `info` prints the V2 record as JSON, including build state.
- **`source enable`, `disable`, `remove`.**
  - `remove` refuses while a daemon runs.
  - It takes `index.lock` and deletes the project data before the
    registry row.
  - It never touches the source files.
- **`index [--source ID]`.**
  - Runs each source under its own `index.lock`, with
    `progress::track("index")`.
  - Prints the reference's per-source line:
    `scanned … linked … embedded`.
  - Also prints, when they apply:
    - the email-attachment line;
    - the scan-incomplete line;
    - the offline line;
    - the back-online line;
    - the summary.
  - Exits with the indexing-partial-failure code when a source or file
    failed.
- **`status [--json] [--watch] [--interval] [--errors] [--verbose]`.**
  - `--json` is the RUST-11 status model plus the `tokenizer` and
    `backend` sections.
  - The text view has the reference's Indexer, Summary, Sources and
    Problems sections as plain text.
  - `--watch` redraws the screen with presentation-only deltas.
  - In server mode the backend section reports that server-side counts
    are not available yet. It never makes numbers up.
- **`docs [--source ID] [--json]`.**
  - Lists every document-kind file in each active build, with format,
    title, page count, attachments, and the section, paragraph and table
    counts.
  - The reference stores those counts. V2 counts the build's chunks by
    kind (`ProjectStore::chunk_kind_counts`), which gives the same
    numbers because chunk parity is already proven.
- **`watch`.** An alias for `daemon run`.

## Supporting changes

- **Finalizer reports.** `BuildFinalizer::finalize` now returns a
  `FinalizeReport` (`linked`, `embedded`):
  - The linker reports the automatic links it inserted, which is the
    reference's `linked`.
  - The embedding finalizer reports the vectors it computed.
  - `SourceResult` sums them.
- **Attachment stats.** `FileKnowledge.attachments` (`AttachmentStats`)
  carries email-attachment outcomes from the converter: seen, indexed,
  skipped (including unsupported types) and failed. These are reported,
  never stored. `SourceResult.attachments` sums them.
- **Back online.** `SourceResult.became_online` reports a source that was
  offline on the previous pass.
- **`config get` on containers.** For a dict or list it now uses Rich's
  pretty-print layout, at width `COLUMNS` (default 80): a node is expanded
  only when its one-line form overflows. This makes `config get storage`
  match the reference.

## Compat gate

- **Result.** All 14 manifest steps owned by RUST-12 or earlier match the
  Python reference, with no canonical differences: init, source add,
  index, reindex, status (before and after indexing), docs, and the
  config steps.
- **Raised phase.** CI's `RUST_PHASE` goes from RUST-09 to RUST-12.
- **New gate option.** `gate --exclude-phase RUST-10` skips the four
  RUST-10 steps, which run CLI query commands that arrive in slice 2.
  Slice 2 removes the exclusion.
- **Volatile key.** `status` now treats `database_size_bytes` as volatile.
  The V2 storage layout differs from the reference's by design (ADR 0006), so file sizes cannot match. Every other metric is still
  compared.
