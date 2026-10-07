# Contributing to RagMonk

RagMonk is a Rust application. The Cargo workspace is the repository root;
each crate's role is listed in [`docs/architecture.md`](docs/architecture.md)
and design decisions are recorded in [`docs/adr/`](docs/adr).

## Development setup

`rust-toolchain.toml` pins the toolchain; `rustup` installs it on first
use. No other runtime is needed.

```bash
cargo build --workspace          # the `ragmonk` binary: target/debug/ragmonk
```

## Running the checks

CI (`.github/workflows/rust.yml`) runs these on Linux, macOS and Windows.
Run them locally before you push:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
cargo xtask branding-audit       # the former product name must not come back
cargo xtask clean-slate-audit    # no migration/legacy/version-naming concepts (ADR 0032)
```

Some test groups only run fully when their external inputs are present.
Without them they skip, unless the matching `RAGMONK_REQUIRE_*` variable
makes skipping a failure, as CI does:

| Inputs | Environment |
|---|---|
| Pinned embedding and reranker models (`scripts/fetch_models.sh DIR`) | `RAGMONK_MODELS_DIR=DIR`, `RAGMONK_REQUIRE_MODELS=1` |
| Pinned OCR models (the `ocrs/` folder of the same script's output) | `RAGMONK_OCR_MODELS_DIR=DIR/ocrs`, `RAGMONK_REQUIRE_OCR=1` |
| A live OpenSearch / Elasticsearch (`docker compose -f docker/docker-compose.opensearch.yml up -d`) | `RAGMONK_TEST_OPENSEARCH_URL` / `RAGMONK_TEST_ELASTICSEARCH_URL`, `RAGMONK_REQUIRE_LIVE_SERVER=1` |

The live-server tests create and delete indices under a unique prefix.
Never point them at a real deployment.

## Fixtures and expected results

`fixtures/` holds the source inputs tests index (code in several
languages, documents, emails, OCR images). `fixtures/expected/` holds the
expected results tests compare against, and
`fixtures/search_quality/gates.json` the retrieval quality floors.
`benchmarks/` holds benchmark records.

Expected results are RagMonk's own specification. A deliberate behaviour
change regenerates the affected file with `RAGMONK_BLESS=1 cargo test
-p <crate> --test <test>` (where the test supports it) or edits it by
hand, in the same PR, and the PR description says why.

## Test isolation

Every test that runs the CLI sets `RAGMONK_HOME` to a temporary directory
and removes inherited `RAGMONK_*` variables. Tests must never read or
write the real `~/.ragmonk`.

## Packaging and the install scripts

`cargo xtask package` builds a release archive for one target and records
it in the output directory's `SHA256SUMS`; `cargo xtask release-manifest`
writes the final `SHA256SUMS` over every archive in a directory.

`install.sh` and `install.ps1` download `ragmonk-<ver>-<target>` for this
platform, verify it against the release's `SHA256SUMS`, unpack it under
`$RAGMONK_INSTALL_DIR/versions` and link `ragmonk` onto `RAGMONK_BIN_DIR`.
Run them locally exactly as CI does, against archives packaged from your
own build:

```bash
cargo build -p ragmonk-cli
cargo xtask package --binary target/debug/ragmonk \
    --version 0.9.0 --target x86_64-unknown-linux-gnu --out /tmp/dist
RAGMONK_VERSION=0.9.0 RAGMONK_DOWNLOAD_BASE=/tmp/dist \
    RAGMONK_INSTALL_DIR=/tmp/ragmonk-install RAGMONK_BIN_DIR=/tmp/ragmonk-bin sh ./install.sh
/tmp/ragmonk-bin/ragmonk version
```

```powershell
$env:RAGMONK_VERSION = "0.9.0"; $env:RAGMONK_DOWNLOAD_BASE = "$env:TEMP\dist"; ./install.ps1
```

CI runs this (install, re-install, `ragmonk update rollback`, a tampered
archive) on all three OSes.

## Releases

Releases are published by `.github/workflows/rust-release.yml` (ADR 0030).
Tags are strict semantic versions, `vMAJOR.MINOR.PATCH`.

## Commit style

Keep commits focused and explain why a change was made as well as what it
does. User-visible changes are described in the release notes.
