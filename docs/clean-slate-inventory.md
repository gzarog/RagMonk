# RagMonk 1.0 clean-slate removal inventory

<!-- clean-slate-audit: allow-start -->
Policy: [ADR 0032](adr/0032-ragmonk-1.0-clean-slate.md). This file covers
each area and its classification. The exact per-file list with counts is
`audit/clean-slate-baseline.tsv`, which `cargo xtask clean-slate-audit`
enforces as shrink-only. Every entry below is removed or neutralized by the
phase named; at release the baseline is empty and this file is deleted.

## Migration / predecessor import — remove (RM10-01, RM10-02)
| Area | What | Phase |
|---|---|---|
| `crates/ragmonk-storage/src/v1.rs` | V1 home/source inventory reader | 01 |
| `crates/ragmonk-storage/src/preflight.rs` | migration preflight | 01 |
| `crates/ragmonk-storage/src/migrate.rs` | `Migration` type/runner, `schema_migrations` | 02 |
| `crates/ragmonk-storage/src/schema.rs` | `CONTROL_MIGRATIONS`, `KNOWLEDGE_MIGRATIONS`, `V2_SCHEMA_VERSION` | 02 |
| `crates/ragmonk-storage/src/{control,knowledge,maintenance,lib}.rs` | `v1_import` origin, `migration_log`, migration exports, `V2Layout` | 01/02 |
| `crates/ragmonk-storage/tests/{v1_import,knowledge_crud}.rs` | V1 import and migration tests | 01/02 |
| `crates/ragmonk-cli/src/migrate_cmd.rs`, `tests/migrate.rs` | `migrate-to-rust-v2` command | 01 |
| `crates/ragmonk-cli/src/{main,ops_cmd,workflow,doctor_cmd}.rs` | command registration, V1/V2 status labels | 01/04 |
| `rust/compat/fixtures/v1_home/` | V1 home fixture | 01 |
| `rust/docs/adr/0028-migrate-execute.md`, `0031-cutover.md` | migration/cutover ADRs | 01/09 |

## Legacy server indexes — remove (RM10-01, RM10-03)
| Area | What | Phase |
|---|---|---|
| `crates/ragmonk-backends/src/legacy.rs` | legacy-prefix discovery and deletion | 01 |
| `crates/ragmonk-backends/src/{schema,backend,engine,lib}.rs` | `ragmonk-v2` `SCHEMA_TAG`, `v2_prefix`, V2-named types | 03 |
| `crates/ragmonk-backends/tests/live.rs` | legacy cleanup and V2 lifecycle tests → current-index lifecycle | 01/03 |
| `.github/workflows/rust.yml` `server-v2-integration` | live migrate job | 01/03 |

## V1/V2 naming in current contracts — rename (RM10-02 … RM10-04)
`ragmonk-core` `ids::v2` → neutral IDs; `paths.rs` `<home>/v2/` → `state/control.db` and
`projects/<id>/`; `V2Layout`; V2 status labels in the CLI, UI and doctor;
"V2" in `ragmonk-indexing`, `ragmonk-ml` manifest comments and ADRs.
`all-MiniLM-L6-v2` / `ms-marco-MiniLM-L-6-v2` are model ids, not product
naming (neutral tokens).

## Python-compatibility emulation — remove or replace (RM10-04, RM10-08)
These emulate Python/pydantic/PyYAML behavior byte-for-byte only to match
the old implementation: `ragmonk-config/src/{coerce,pyvalue,loader}.rs`,
`ragmonk-ai/src/pyfmt.rs`, `ragmonk-telemetry/src/logging.rs`
(`python_isoformat`, `python_json_str`), the "matching the Python reference"
`ragmonk-core/src/paths.rs` helpers, `ragmonk-indexing/src/ignore.rs`
(`fnmatch`), and `ragmonk-documents`/`ragmonk-convert` comments.
Behavior that is still the right product behavior stays and is
re-documented on its own terms; quirks kept only for compatibility go.

## Rewrite harness and Python tooling — remove (RM10-06, RM10-08)
| Area | What | Phase |
|---|---|---|
| `rust/compat/golden/**` | goldens captured from the Python implementation | 08 (replaced by product fixtures) |
| `rust/compat/benchmarks/python-*` and `baseline-summary.json` | Python performance baselines | 06 |
| `rust/compat/fixtures/**` | useful inputs move to `fixtures/` | 06/08 |
| `*/bin/ragmonk-*-bench.rs` | "time the Python reference" notes | 06 |
| `rust/scripts/package_release.py` | → `cargo xtask package` | 06 |
| `scripts/check_branding.py` | → `cargo xtask branding-audit` | 06 |
| `.github/workflows/*.yml` | `python3`/`python` calls, rewrite PR-base guard, `rust-v*` tags | 04/06/09 |
| `install.sh`, `install.ps1` | old-venv detection and `migrate-to-rust-v2` advice | 04 |
| `README.md`, `CONTRIBUTING.md`, `rust/README.md`, `docs/**`, `.gitignore`, `.dockerignore` | rewrite-era instructions | 04/09 |

## Python — allowed (source-language support only)
Permanently allowlisted in `xtask/src/audit.rs`:
* `crates/ragmonk-code/src/lang.rs`, `src/framework.rs`, `queries/python.scm` —
  Python parser wiring and route-decorator detection;
* the `tree-sitter-python` grammar crate (neutral token);
* `.py` files under `fixtures/` (today `rust/compat/fixtures/`) and the
  `code/python/` fixture trees.

Other Python mentions in tests that exercise Python *source indexing*
(e.g. `ragmonk-code` tests, graph fixtures) are moved to `fixtures/code/python/`
or given a narrowly scoped allowlist entry when their phase rewrites them.

## Release tags
`rust-v*` pre-release tags in `rust-release.yml` → one strict semver
`v<MAJOR.MINOR.PATCH>[-pre]` convention shared with the installers and
`ragmonk update` (RM10-09).
<!-- clean-slate-audit: allow-end -->
