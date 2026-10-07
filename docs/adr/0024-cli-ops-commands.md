# ADR 0024: CLI operations commands (RUST-12, slice 3)

Status: accepted

## Decisions (user)

1. Backups are V2-native. Restoring a Python V1 archive is refused, with
   directions.
2. `doctor` and `health` include every applicable section. The
   AI-provider section is left out because AI features are outside
   RUST-12.
3. The slice ships as one PR.

## Commands

### `doctor` and `health`

- **Sections, check names and verdict rules** are the reference's. The
  overall result is `UNHEALTHY` on any fail and `HEALTHY WITH WARNINGS`
  on any warn.
- **Checks:**
  - Core: version.
  - Database (the V2 control plane): SQLite, WAL mode, schema version
    equal to the latest.
  - Sources: reachability of enabled sources.
  - Index Lock: free, held (with owner) or unknown.
  - Index: queue depth from the status model.
  - Disk: free space on the home's filesystem, through `sysinfo`.
  - Semantic (when enabled): vectors under the current model fingerprint
    and the HNSW index size.
  - Tokenizer: identity, limits, and every stored chunk embedding payload
    measured with special tokens. Any payload over the model limit is a
    fail.
  - Server (in server mode):
    - the engine and the endpoint, with credentials redacted;
    - connectivity, through `ServerBackend::connect`;
    - index existence, through the new `ServerBackend::index_status`
      (`HEAD` only, never creates anything).
- **Exit code.** An unhealthy result exits with the health-check code and
  no extra error line. The CLI now treats an empty error message as a
  silent exit.

### `backup [DEST] [--json]`

- **Locking.** Runs under `index.lock`.
- **Contents.** A `.tar.gz` holding:
  - `manifest.json`;
  - `v2/control.db`;
  - `v2/projects/<id>/knowledge.db` for each indexed project;
  - `config.yaml`.
- **Snapshots.** Databases are copied with SQLite's `VACUUM INTO` over a
  read-only connection. The copy is consistent while a writer is active,
  and it never migrates the source database. `upgrade` relies on that.
- **Manifest.** It keeps the reference's keys (`ragmonk_version`,
  `created_at`, `sources_schema_version`, now the control-plane version,
  `projects`, `sources`) and adds `format_version: 2` and `storage: "v2"`.
- **Not archived.** ANN index files are caches. Search falls back to
  exact matching until the next sync rebuilds them.

### `restore ARCHIVE [--json]`

- **Order of steps.** Nothing in the live home is touched until
  everything has been verified:
  1. Extract into a scratch staging directory. The `tar` crate's `unpack`
     refuses entries that would escape it.
  2. Read the manifest.
  3. Refuse a Python V1 archive (`format_version` 1, no `storage`), and
     point to the Python restore followed by
     `migrate-to-rust-v2 --import-sources`.
  4. Refuse newer formats and schemas.
  5. Run `PRAGMA integrity_check` on every database.
  6. Stop a running daemon.
  7. Move the live `v2/` directory and config aside, then rename the
     staged ones into place.
  8. Restart the daemon if it was running.
- **Rollback.** A failed swap puts the previous state back.

### `rebuild [--source ID] [--fresh] [--yes] [--json]`

- **How it works.** Each selected source is marked for a full rebuild,
  then runs through the normal coordinator under `index.lock` and
  `progress.track("rebuild")`.
- **Always recoverable.** A V2 full build is written next to the visible
  build and published only on success, so a failed rebuild never leaves a
  source without a usable index. The reference offers that only with
  `--fresh`. Here `--fresh` adds the reference's reachability check and
  confirmation prompt.
- **Output and exit code.** The output lines match the reference. A file
  failure exits with the partial-failure code.

### `upgrade [--json]`

- **Before.** Reads the schema versions of the control plane and each
  project read-only, so no migration is applied.
- **If anything is pending.** Takes a `backup` first.
- **Migrate.** Opens everything, which applies the migrations.
- **Report.** Shows before and after versions, with the database named
  `control` instead of the reference's `sources`, and runs the `doctor`
  verdict.
- **Rollback.** No automatic rollback, as in the reference.

### `uninstall [--keep-data] [-y] [--json]`

- **Data.** Stops a running daemon, then deletes `RAGMONK_HOME`, unless
  `--keep-data` is given.
- **The binary.** The Rust preview is a standalone binary, and a running
  executable cannot delete itself on Windows. So `install_method` is
  `standalone_binary`, `app_removed` is false, and the output includes
  manual removal instructions. The reference's pip, pipx and
  install-script paths do not apply.

### `vectors rebuild` and `vectors backfill`

- **`rebuild`** drops each project's HNSW file and rebuilds it from the
  vectors stored under the current model's fingerprint. A source with no
  vectors reports `backend: null`.
- **`backfill`** embeds every subject missing a current vector
  (`embed_build`) and syncs the index. It needs the embedding model, and
  reports a usage error when the model is not installed.

## Storage additions

- **`ragmonk_storage::maintenance`:**
  - read-only schema versions;
  - latest known versions;
  - `integrity_check`;
  - `snapshot` (`VACUUM INTO`);
  - the source list read without migrating;
  - the journal mode.

## Tests

- **`ragmonk-cli/tests/ops.rs`:**
  - doctor and health sections, verdict and text;
  - a backup → change → restore round trip, after which status and
    queries work;
  - V1, garbage and missing archives are refused and leave the home
    untouched;
  - rebuild (plain and `--fresh`), upgrade with nothing pending,
    `vectors rebuild` without embeddings, and uninstall with and without
    `--keep-data`.
- **Unit tests:** maintenance helpers, the doctor verdict rules.
- **Compat gate.** All 23 RUST-12 steps still match the reference.
