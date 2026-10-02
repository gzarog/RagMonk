# RagMonk Rust rewrite (work in progress)

This workspace hosts the Python→Rust rewrite tracked by the
`RagMonk_Full_Rust_Rewrite_V1` plan. **The Python application under `src/`
is still the product**; nothing here changes its behavior yet.

| Crate | Status |
|-------|--------|
| `ragmonk-cli` | `ragmonk` binary; only `version` so far (RUST-00 skeleton) |
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

`--strict-ids` keeps path-derived source/project IDs for same-machine runs;
the default *portable* mode masks them. The harness only resets work dirs
that carry its own marker file and opens SQLite read-only.

Policies: [`docs/adr/`](docs/adr). Baselines: `compat/baseline/`,
benchmarks: `compat/benchmarks/`.
