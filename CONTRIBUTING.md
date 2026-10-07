# Contributing to RagMonk

RagMonk is a native Rust application. The Cargo workspace lives in
[`rust/`](rust/README.md). Each crate's role is listed there, and the
design decisions are recorded in [`rust/docs/adr/`](rust/docs/adr).

## Development setup

`rust/rust-toolchain.toml` pins the toolchain; `rustup` installs it on
first use. No other runtime is needed.

```bash
cd rust
cargo build -p ragmonk-cli          # the `ragmonk` binary: target/debug/ragmonk
```

## Running the checks

CI (`.github/workflows/rust.yml`) runs these on Linux, macOS and Windows.
Run them locally before you push:

```bash
cd rust
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
python3 ../scripts/check_branding.py   # no identifiers from before the rename
```

Some test groups only run fully when their external inputs are present.
Without them they skip, unless the matching `RAGMONK_REQUIRE_*` variable
makes skipping a failure, as CI does:

| Inputs | Environment |
|---|---|
| Pinned embedding and reranker models (`rust/scripts/fetch_models.sh DIR`) | `RAGMONK_MODELS_DIR=DIR`, `RAGMONK_REQUIRE_MODELS=1` |
| Pinned OCR models (the `ocrs/` folder of the same script's output) | `RAGMONK_OCR_MODELS_DIR=DIR/ocrs`, `RAGMONK_REQUIRE_OCR=1` |
| A live OpenSearch / Elasticsearch (`docker compose -f docker/docker-compose.opensearch.yml up -d`) | `RAGMONK_TEST_OPENSEARCH_URL` / `RAGMONK_TEST_ELASTICSEARCH_URL`, `RAGMONK_REQUIRE_LIVE_SERVER=1` |

The live-server tests create and delete indices under a unique prefix.
Never point them at a real deployment.

## Golden reference data

`rust/compat/golden/`, `rust/compat/fixtures/` and `rust/compat/benchmarks/`
hold expectations captured from the former Python implementation. They are
frozen: tests compare against them, and nothing regenerates them. A
deliberate behaviour change updates the affected golden file in the same
PR, and the PR description says why. See ADR 0031.

## Test isolation

Every test that runs the CLI sets `RAGMONK_HOME` to a temporary directory
and removes inherited `RAGMONK_*` variables. Tests must never read or
write the real `~/.ragmonk`.

## The install scripts (`install.sh` / `install.ps1`)

`install.sh` and `install.ps1` at the repo root are what `README.md`'s
`curl | sh` / `irm | iex` one-liners run. They install the native Rust
binary (RUST-15, `rust/docs/adr/0029-native-update.md`): download
`ragmonk-<ver>-<target>` for this platform, verify it against the
release's `SHA256SUMS`, unpack it under `$RAGMONK_INSTALL_DIR/versions`
and link `ragmonk` onto `RAGMONK_BIN_DIR`. Run them locally exactly as CI
does, against archives packaged from your own build:

```bash
cargo build --manifest-path rust/Cargo.toml -p ragmonk-cli
python3 rust/scripts/package_release.py --binary rust/target/debug/ragmonk \
    --version 0.9.0 --target x86_64-unknown-linux-gnu --out /tmp/dist
RAGMONK_VERSION=0.9.0 RAGMONK_DOWNLOAD_BASE=/tmp/dist \
    RAGMONK_INSTALL_DIR=/tmp/ragmonk-install RAGMONK_BIN_DIR=/tmp/ragmonk-bin sh ./install.sh
/tmp/ragmonk-bin/ragmonk version
```

```powershell
$env:RAGMONK_VERSION = "0.9.0"; $env:RAGMONK_DOWNLOAD_BASE = "$env:TEMP\dist"; ./install.ps1
```

CI runs this (install, re-install, `ragmonk update rollback`, a tampered
archive) as blocking jobs on all three OSes -- see
`.github/workflows/rust.yml`.

## Releases

Releases are published by `.github/workflows/rust-release.yml` (ADR 0030,
ADR 0031):

- **On `main`:** after CI passes on a push to `main`, the next `vX.Y.Z` is
  tagged and released. The patch number goes up by default; put
  `[release minor]` in the merge commit message to bump the minor version
  instead.
- **Dry run:** pushing a `rust-vX.Y.Z` tag publishes a pre-release with the
  image.

## Branches

Work on the rewrite release branch targets `release/rust-rewrite-v1`
(enforced by the `Rewrite PR base guard` CI job). Promotion to `main` is a
separate maintainer decision (ADR 0004).

## Commit style

Keep commits focused and explain why a change was made as well as what it
does. User-visible changes are described in the release notes.
