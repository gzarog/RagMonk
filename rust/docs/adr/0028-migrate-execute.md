# ADR 0028: `migrate-to-rust-v2 --execute` (RUST-15, slice 1)

Status: accepted

## Decisions (user)

RUST-15 ships in three slices:

1. this migration command;
2. native `update` and the installers;
3. the release artifacts and the Python-free container.

The release targets are Linux x86_64, macOS aarch64, macOS x86_64 and
Windows x86_64. Model assets are bundled in each archive. Releases get
SHA256 checksums plus optional signing hooks.

## Command

### `--check` (read-only)

`--check` extends ADR 0006's report with an `execute` plan. It never
opens or migrates the control plane: V2 sources are read through a
read-only connection.

The plan lists:

- the V2 local index state to delete: every `v2/projects/*` directory;
- in server mode, the legacy V1 indexes with their document counts (the
  exact names from ADR 0007's discovery);
- the V2 indexes to create or verify;
- the sources that will need a full rebuild;
- the rollback note;
- a plan fingerprint: a hash of the sources, the project directories and
  the legacy-index fingerprint.

### `--execute`

`--execute` needs confirmation, and refuses without it with exit code 2,
changing nothing:

- **Interactively:** type `MIGRATE` after the plan is shown again.
- **Otherwise:** pass `--confirm <fingerprint>` from a check.

A fingerprint that no longer matches the current plan is refused, so a
plan that changed since it was reviewed is never executed. `--execute`
also refuses while the daemon runs, and holds the `index` lock throughout.

It then runs these steps, in this order:

1. **Import** the V1 source definitions into the control plane. This is
   idempotent and leaves the V1 files untouched (they are removed in
   RUST-16).
2. **Server mode only:**
   - `legacy::delete_legacy` deletes the exact legacy indexes. It
     re-checks their fingerprint, so a set that changed is refused.
   - `ServerBackend::init` creates or verifies the V2 indexes.
3. **Delete** all V2 index-derived local state (`v2/projects/*`).
4. **Reset every source:** `ControlPlane::reset_index_state` clears the
   active, pending and recorded builds and versions, and sets
   `needs_full_rebuild` (reason `migrate-to-rust-v2`). Registration,
   patterns and scan state stay.

The report is logged to the control plane's `migration_log` and printed
(`--json` available). The next step is `ragmonk index`.

## Rollback

No side-by-side legacy-index rollback is kept (ADR 0006/0007). The
report and `--check` both state it: returning to the Python release after
`--execute` means rebuilding its V1 indexes from the original sources.

## Verification

`crates/ragmonk-cli/tests/migrate.rs` covers two scenarios.

**Local mode:**

- the check's plan and fingerprint;
- refusals without a terminal or with a stale fingerprint, with nothing
  changed;
- `--confirm` without `--execute` is a usage error;
- execution deletes the project data, keeps the registration, leaves
  queries empty until `ragmonk index` rebuilds.

**Live, in the CI cluster job** (OpenSearch 2.15 and Elasticsearch 8.15):

- legacy indexes are created under a unique prefix, with documents;
- the check reports them;
- `--execute` deletes exactly them and creates the V2 indexes.
