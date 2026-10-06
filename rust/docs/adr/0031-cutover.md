# ADR 0031: Cutover, retiring the Python reference (RUST-16)

Status: accepted (slice 1 of 2)

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

## Slice 2 (next)

Slice 2 deletes:
- `src/`, `tests/`, `pyproject.toml`, the Python benchmarks and scripts;
- `ci.yml`, `release.yml` and `main-release.yml`.

It also makes `rust-release.yml` the release path: `v*.*.*` tags, full
releases by default, and the auto-version step from `main-release.yml`. It
updates the top-level README, CONTRIBUTING and docs.
