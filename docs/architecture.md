# RagMonk (Rust workspace)

RagMonk is a native Rust application, and this workspace is the whole
product. The rewrite plan (`RagMonk_Full_Rust_Rewrite_V1`, RUST-00 to
RUST-16) ported the former Python implementation, and the cutover in
[ADR 0031](docs/adr/0031-cutover.md) removed that implementation from the
repository.

| Crate | Status |
|-------|--------|
| `ragmonk-cli` | `ragmonk` binary: `version`, `config show/get/set`, `server schema/init`, `daemon start/stop/restart/status/run`, `init`, `source add/list/info/enable/disable/remove`, `index`, `status`, `docs`, `watch`, `search`, `symbol`, `callers`, `callees`, `references`, `impact`, `explore`, `link add/remove/list`, `doctor`, `health`, `backup`, `restore`, `rebuild`, `upgrade`, `uninstall`, `vectors rebuild/backfill` (RUST-12); `serve --mcp`, the stdio MCP server with the `ragmonk_*` tools (RUST-13); `ask` and `ai providers/status/login/logout/models`, and `ui`, the local Admin UI (RUST-14); `update check/status/install/rollback`, the background check and startup notice (RUST-15) |
| `ragmonk-ai` | AI providers for `ask`: OpenAI, Anthropic, Ollama, OpenAI-compatible, Codex (stdio JSON-RPC) and Copilot (CLI) (RUST-14) |
| `ragmonk-backends` | V2 OpenSearch/Elasticsearch schema, bounded bulk, atomic build publication, legacy cleanup (RUST-03) |
| `ragmonk-code` | Tree-sitter code intelligence: reference `.scm` queries, extraction, resolution, framework rules, code graph, whole-build cross-file resolution (RUST-05) |
| `ragmonk-documents` | canonical normalized-document model, exact pinned tokenizer, token-budget splitting, row-aware tables, payload-aware chunker (RUST-06) |
| `ragmonk-convert` | Rust-native document conversion (docling.rs, no ML/network), Docling-JSON normalizer, V2 document processor and full registry (RUST-07) |
| `ragmonk-knowledge` | Cross-domain linker (code entities <-> document chunks, five matchers, Python-parity boundaries) and persistent manual links (RUST-08) |
| `ragmonk-ml` | Pinned model assets, pure-Rust (Candle) embeddings, embedding cache, persistent HNSW index, semantic search, RRF and the cross-encoder reranker (RUST-09) |
| `ragmonk-retrieval` | Lexical search, hybrid RRF fusion with exact-match pinning, the optional cross-encoder pass, symbol search, graph queries, explore and impact (RUST-10); search context expansion and query classification (RUST-12) |
| `ragmonk-core` | errors/exit codes, domain models, home layout, stable IDs, secret filter, path guard, version (RUST-01) |
| `ragmonk-config` | `config.yaml` + env layering with PyYAML/pydantic-exact semantics (RUST-01) |
| `ragmonk-indexing` | scanner, ignore rules, incremental diff, retries, run lock, bounded transactional V2 coordinator (RUST-04); progress snapshot and status verdicts, daemon worker, reconciliation, PID and health files, filesystem watcher, network polling, targeted passes (RUST-11) |
| `ragmonk-storage` | V2 SQLite control plane + per-source knowledge store (RUST-02) |
| `ragmonk-update` | Native self-update: strict release tags, `update.json` cache, SHA-256 + optional minisign verification, `versions/<ver>` + `current` layout with rollback (RUST-15); see ADR 0029 and `scripts/package_release.py`; releases (`rust-release.yml`) and the `rust/Dockerfile` image: ADR 0030 |
| `ragmonk-telemetry` | JSON-lines logging, URL/credential redaction (RUST-01) |

## Quality gates
```sh
cd rust
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
```

## Reference data (frozen)
The Python reference that `compat/golden/` and `compat/benchmarks/` were
captured from has been retired (RUST-16, [ADR 0031](docs/adr/0031-cutover.md)). The goldens are now
frozen expectations: tests compare against them, and nothing regenerates
them. The `ragmonk-compat` harness and the `compat/tools/gen_*.py`
generators are still in git history before ADR 0031.

`compat/fixtures/corpus/` holds the code and document corpus that the MCP,
Admin UI and `ask` golden tests index. It is a copy of the former Python
`tests/fixtures` files those goldens were captured from.

Policies: [`docs/adr/`](docs/adr) (storage: ADR 0006, server: ADR 0007, V3 clean-slate plan). Goldens: `compat/golden/`,
benchmarks: `compat/benchmarks/`.
