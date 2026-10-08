# ADR 0022: CLI core workflow

Status: accepted

## Decisions

1. This ADR covers the core workflow commands: `init`, `source …`,
   `index`, `status`, `docs` and `watch`. Query commands are ADR 0023,
   operations commands ADR 0024.
2. `--json` payloads are a stable contract for agents and scripts. Text
   output is plain text, without colors, panels or box tables.

## Commands

- **`init`.**
  - Writes the default config, or validates an existing one.
  - Creates the control plane.
  - `--storage-mode server` validates the cluster before writing anything:
    - it refuses URLs that contain credentials;
    - it then connects through `ServerBackend::connect`, which checks
      reachability, authentication and engine identity.
- **`source add`.**
  - Rejects paths that do not exist or are not a directory.
  - Uses the path-derived `src_<sha256[:10]>` id.
  - Adding the same path twice returns the existing source.
- **`source list` and `source info`.** `list` prints one row per source.
  `info` prints the record as JSON, including build state.
- **`source enable`, `disable`, `remove`.**
  - `remove` refuses while a daemon runs.
  - It takes `index.lock` and deletes the project data before the
    registry row.
  - It never touches the source files.
- **`index [--source ID]`.**
  - Runs each source under its own `index.lock`, with
    `progress::track("index")`.
  - Prints a per-source line:
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
  - `--json` is the status model (ADR 0019) plus the `tokenizer` and
    `backend` sections.
  - The text view has Indexer, Summary, Sources and Problems sections.
  - `--watch` redraws the screen with presentation-only deltas.
  - In server mode the backend section reports that server-side counts
    are not available yet. It never makes numbers up.
- **`docs [--source ID] [--json]`.**
  - Lists every document-kind file in each active build, with format,
    title, page count, attachments, and the section, paragraph and table
    counts.
  - The counts come from the build's chunks by kind
    (`ProjectStore::chunk_kind_counts`); nothing extra is stored.
- **`watch`.** An alias for `daemon run`.

## Supporting changes

- **Finalizer reports.** `BuildFinalizer::finalize` returns a
  `FinalizeReport` (`linked`, `embedded`):
  - The linker reports the automatic links it inserted (`linked`).
  - The embedding finalizer reports the vectors it computed.
  - `SourceResult` sums them.
- **Attachment stats.** `FileKnowledge.attachments` (`AttachmentStats`)
  carries email-attachment outcomes from the converter: seen, indexed,
  skipped (including unsupported types) and failed. These are reported,
  never stored. `SourceResult.attachments` sums them.
- **Back online.** `SourceResult.became_online` reports a source that was
  offline on the previous pass.
- **`config get` on containers.** A dict or list is pretty-printed at
  width `COLUMNS` (default 80): a node is expanded only when its one-line
  form overflows.

## Evidence

CLI tests run `init`, `source add`, `index`, a re-index, `status` (before
and after indexing), `docs` and the config commands against the expected
outputs in `fixtures/expected/`. Snapshot ids, run ids, hosts, timestamps
and request counts of the status report are volatile and excluded from
comparisons; every counter is compared.
