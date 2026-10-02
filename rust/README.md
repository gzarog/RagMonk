# RagMonk Rust rewrite (work in progress)

This workspace hosts the Python→Rust rewrite tracked by the
`RagMonk_Full_Rust_Rewrite_V1` plan. **The Python application under `src/`
is still the product**; nothing here changes its behavior yet.

| Crate | Status |
|-------|--------|
| `ragmonk-cli` | `ragmonk` binary: `version`, `config show/get/set`, `migrate-to-rust-v2 --check/--import-sources`, `server-v2 schema/init/legacy` |
| `ragmonk-backends` | V2 OpenSearch/Elasticsearch schema, bounded bulk, atomic build publication, legacy cleanup (RUST-03) |
| `ragmonk-code` | Tree-sitter code intelligence: reference `.scm` queries, extraction, resolution, framework rules, code graph, whole-build cross-file resolution (RUST-05) |
| `ragmonk-core` | errors/exit codes, domain models, home layout, stable IDs, secret filter, path guard, version (RUST-01) |
| `ragmonk-config` | `config.yaml` + env layering with PyYAML/pydantic-exact semantics (RUST-01) |
| `ragmonk-indexing` | scanner, ignore rules, incremental diff, retries, run lock, bounded transactional V2 coordinator (RUST-04) |
| `ragmonk-storage` | V2 SQLite control plane + per-source knowledge store, versioned migrations with backup, read-only V1 preflight/import (RUST-02) |
| `ragmonk-telemetry` | JSON-lines logging, URL/credential redaction (RUST-01) |
| `ragmonk-compat` | Python-vs-Rust differential harness; removed at RUST-16 |

## Quality gates
```sh
cd rust
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
```

## Compatibility harness
`compat/manifest.json` lists scenarios (fixtures copied from `tests/fixtures`
into an isolated work dir, then CLI steps with `RAGMONK_HOME` inside it; no
inherited `RAGMONK_*` variables, update checks off). Run from the repo root:

```sh
cargo build --manifest-path rust/Cargo.toml -p ragmonk-compat -p ragmonk-cli
# Prove the Python reference is reproducible (RUST-00 gate)
rust/target/debug/ragmonk-compat stability --impl python --program .venv/bin/ragmonk
# Capture the Rust candidate and diff against the committed baseline
rust/target/debug/ragmonk-compat capture --impl rust --program rust/target/debug/ragmonk --out rust.json
rust/target/debug/ragmonk-compat compare rust/compat/baseline/python-1e92ed4.json rust.json
```

Phase gate (every step owned by the phase or earlier must match):

```sh
rust/target/debug/ragmonk-compat gate python.json rust.json --phase RUST-01
```

Golden fixtures for pure functions (config parsing, IDs, redaction) live in
`compat/golden/` and are regenerated with
`python rust/compat/tools/gen_core_golden.py` (Python reference installed).

`--strict-ids` keeps path-derived source/project IDs for same-machine runs;
the default *portable* mode masks them. The harness only resets work dirs
that carry its own marker file and opens SQLite read-only.

Policies: [`docs/adr/`](docs/adr) (storage: ADR 0006, server: ADR 0007, V3 clean-slate plan). Baselines: `compat/baseline/`,
benchmarks: `compat/benchmarks/`.
