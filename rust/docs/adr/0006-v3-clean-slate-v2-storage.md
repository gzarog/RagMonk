# ADR 0006 — Clean-slate Rust V2 storage (V3 plan) and the RUST-02 control plane

Status: accepted (RUST-02)

## Context
From RUST-02 onward the governing plan is
`RagMonk_Full_Rust_Rewrite_V3_Clean_Slate` (project decision, 2026-10-02).
Python V1 is a behavioral/quality reference, not a storage-format
contract. The project owner also decided that the explicit, confirmed
migration deletes legacy RagMonk server indexes **before** the V2 rebuild,
with no index-level rollback (rollback to Python requires a V1 rebuild from
source). ADR 0001's byte-compatibility rules keep applying to user-facing
contracts (config, CLI exit codes, external JSON); they no longer apply to
index-derived storage.

## Decision
* **Separate V2 tree.** V2 state lives in `<home>/v2/` (`control.db`,
  `projects/<project_id>/knowledge.db`). Python's `sources.db` and
  `projects/*/knowledge.db` are opened strictly read-only and never modified
  or deleted by RUST-02, so the Python reference keeps working until cutover.
  (Read-only access to a WAL database may leave empty `-wal`/`-shm`
  sidecars; this is SQLite's coordination with a possibly live Python
  writer, and the database bytes are unchanged — tested.)
* **What is imported:** source definitions only — path, type, enabled flag,
  include/exclude patterns, creation time. Source IDs keep the
  path-derived `src_` + sha256[:10] algorithm. `config.yaml` is read as-is.
* **What is never imported:** V1 per-file state, jobs, errors, entities,
  relationships, documents/sections, links, embeddings, vector items,
  conversion/embedding caches and source scan state.
* **Mandatory first V2 build.** Every imported or new source starts as
  `needs_full_rebuild`. `control::plan_for` returns `Full` unless the source
  has a published V2 build whose recorded `IndexVersions` (schema, parser,
  chunker, converter, embedding model, embedding text version) equal the
  running code's; `ProjectStore::reusable_files` returns nothing for a full
  plan. Old state therefore cannot suppress processing.
* **Atomic local publication.** Every index-derived row carries its
  `build_id`; readers query the source's `active_build_id`. A build becomes
  visible only via `publish_build`; an aborted first build leaves the source
  in `needs_full_rebuild`, an aborted later build keeps the previous build
  visible. Unchanged files are carried forward into incremental builds;
  superseded/aborted builds are garbage-collected.
* **V2 IDs** (`ragmonk_core::ids::v2`): 32-hex sha256 over a domain tag and
  natural-key parts — deterministic and idempotent within V2, not V1-compatible.
* **Migrations** are versioned, transactional, refuse newer schemas, and a
  database that already holds data is copied (`VACUUM INTO`) to
  `<home>/backups/v2-migrations/` before any pending migration runs.
* **CLI.** `ragmonk migrate-to-rust-v2 --check [--json]` (read-only report)
  and `--import-sources` (non-destructive, idempotent). The destructive
  `--execute` (legacy server-index deletion after confirmation) belongs to
  RUST-15 per the plan.
* **Manual links** are stored as user data (`manual_links`, keyed by
  qualified name/relative path) so they survive rebuilds.

## Consequences
Rust V2 and Python V1 can coexist on one home during development. Disk use
grows until the RUST-15/16 cleanup removes V1 data.
