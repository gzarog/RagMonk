# ADR 0031: Cutover, retiring the Python reference (RUST-16)

Status: accepted

## Decisions (user)

- **Python code:** deleted, not archived. Git history keeps it. Fixtures that
  the Rust tests still read are moved under `rust/` first.
- **Layout:** the Rust workspace stays in `rust/`.
- **Releases:** `rust-release.yml` becomes the release workflow. The Python
  `release.yml` and `main-release.yml` go away.
- **Delivery:** two PRs.
  1. Retire the compatibility harness: Rust-only, goldens frozen.
  2. Delete the Python application and its CI/release workflows, then
     update the docs.

## Slice 1: retire the compatibility harness

**Removed:**
- The `ragmonk-compat` crate: its scenario runner, capture, compare and
  phase gate.
- `rust/compat/manifest.json`.
- `rust/compat/baseline/` (the Python capture at `1e92ed4`).
- The `rust/compat/tools/gen_*.py` golden generators.
- The `compat-baseline` CI job, which installed the Python reference to
  prove it was stable.

**Removed test:**
- `ragmonk-code/tests/queries.rs`, which asserted that the shipped `.scm`
  queries matched the Python copies. It was written to retire here: once the
  Python tree is gone it has nothing to compare against, and its own guard
  would turn it into a no-op. The queries embedded in `ragmonk-code/queries/`
  are now the source of truth.

**Kept:**
- `rust/compat/golden/`, `rust/compat/fixtures/` and `rust/compat/benchmarks/`,
  as frozen reference data. Every golden test still compares against them;
  they are no longer regenerated. Test module comments that name a
  `gen_*.py` generator describe where a golden came from. The generator is
  in git history.

**Moved:**
- The MCP, Admin UI and `ask` golden tests index a corpus that used to be
  read from the Python `tests/fixtures/`. Those files are copied, byte for
  byte, to `rust/compat/fixtures/corpus/`. The Python suite keeps its copy
  until slice 2 deletes it.
- These tests were run with `tests/fixtures/` removed, and they pass.

## Slice 2: remove the Python application

**Additional decisions (user):**
- Release auto-versioning is ported to the Rust CI.
- The branding audit and the `docker/` dev-cluster files are kept.
- `docs/` is rewritten for the native binary.
- `CHANGELOG.md` is dropped. Release notes live on the GitHub releases.

**Deleted:**
- `src/`, `tests/` and `pyproject.toml`;
- the Python benchmark harnesses in `benchmarks/`;
- `scripts/generate_release_artifacts.py` and `scripts/refresh_tokenizer_assets.py`;
- `CHANGELOG.md`;
- `docs/v2_completion_report.md` and `docs/indexing_benchmarks.md`;
- the workflows `ci.yml`, `release.yml` and `main-release.yml`;
- the `.gitattributes` rule for the Python tokenizer assets.

**CI:**
- `rust.yml` is now named **CI**.
- It also runs on pushes to `main`, which is what drives releases.
- It gains a `Branding audit` job that runs `scripts/check_branding.py`, a
  standard-library Python script, as CI tooling only. The audit's
  `CHANGELOG.md` exemption is removed with the file.

**Releases (`rust-release.yml`):**

| Trigger | Result |
|---|---|
| CI passed on a push to `main` (`workflow_run`) | Computes the next `vX.Y.Z` from the latest strict `v*.*.*` tag, using the same rule as the old `main-release.yml`: patch bump, or minor bump with `[release minor]` in the merge commit message. Pushes the tag before the builds start, so two racing merges cannot both claim it. Publishes a **full** release with the image. |
| `rust-v*` tag push | A pre-release, for dry runs. |
| Manual dispatch | As before. |

- Every job builds, labels and targets the commit CI validated
  (`workflow_run.head_sha`), not whatever `main` points to by the time the
  job starts.
- With full releases, GitHub's "latest release", and therefore
  `ragmonk update`, starts following native releases from the first
  automatic release on `main`.

**Docs:**
- `README.md`: install has no Python requirement and adds a Docker row.
- `CONTRIBUTING.md` is rewritten for the Cargo workspace: checks, optional
  model and live-server test inputs, frozen goldens, installer testing,
  releases.
- `docs/reference.md` notes that `documents.pdf_table_structure` and
  `pdf_process_workers` are accepted but ignored by the native PDF
  converter. These two config keys were kept for config compatibility.
- `docs/providers`: the Copilot provider runs the `copilot` CLI, not a
  Python SDK. Two design-record pages are marked historical, because their
  file evidence points at the retired Python tree.

**Not changed:**
- `install.sh`/`install.ps1` still print a hint about an old `venv/`.
  Upgrading users need that hint.
- Promotion of `release/rust-rewrite-v1` to `main` remains a separate
  maintainer decision (ADR 0004).
