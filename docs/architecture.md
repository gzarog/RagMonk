# RagMonk architecture

RagMonk is a native Rust application. This Cargo workspace is the whole
product: one `ragmonk` binary built from the crates below, plus the `xtask`
helper for packaging and repository audits.

| Crate | Responsibility |
|-------|----------------|
| `ragmonk-cli` | `ragmonk` binary: `version`, `config`, `server schema/init`, `daemon`, `init`, `source`, `index`, `status`, `docs`, `watch`, `search`, `symbol`, `callers`, `callees`, `references`, `impact`, `explore`, `link`, `doctor`, `health`, `backup`, `restore`, `rebuild`, `update`, `uninstall`, `vectors`, `ask`, `ai`, `ui` and `serve --mcp` |
| `ragmonk-service` | application services shared by the CLI, Admin UI and MCP server: the single storage-mode resolver (`backend::open`), source catalog, bounded parallel indexing runs, server-mode staged indexing and publication, queries, status, `ask`, daemon control |
| `ragmonk-ops` | operations: `doctor`, backup/restore, full rebuilds, vector maintenance, uninstall |
| `ragmonk-mcp` | stdio MCP server (`ragmonk serve --mcp`) exposing the read-only `ragmonk_*` tools |
| `ragmonk-ui` | local Admin UI (`ragmonk ui`), an axum server with embedded templates |
| `ragmonk-ai` | AI providers for `ask`: OpenAI, Anthropic, Ollama, OpenAI-compatible, Codex (stdio JSON-RPC) and Copilot (CLI) |
| `ragmonk-backends` | OpenSearch/Elasticsearch index set with strict mappings: the authoritative server catalog, writer leases with fencing, bounded adaptive bulk writes, server-side copy-forward, atomic per-source build publication with grace-period GC, and the `ServerReader` implementation of the knowledge read port |
| `ragmonk-code` | tree-sitter code intelligence: `.scm` queries, extraction, resolution, framework rules, code graph, cross-file resolution |
| `ragmonk-documents` | normalized-document model, pinned tokenizer, token-budget splitting, row-aware tables, chunker |
| `ragmonk-convert` | Rust-native document conversion (docling.rs, no ML/network), Docling-JSON normalizer, email and attachments, document processor |
| `ragmonk-knowledge` | cross-domain linker (code entities <-> document chunks) and persistent manual links |
| `ragmonk-ml` | pinned model assets, pure-Rust (Candle) embeddings, embedding cache, persistent HNSW index, semantic search, RRF and the cross-encoder reranker |
| `ragmonk-retrieval` | lexical and hybrid search, exact-match pinning, reranking, symbol search, graph queries, explore and impact, context expansion and query classification |
| `ragmonk-indexing` | scanner, ignore rules, incremental diff, retries, per-source run locks, bounded parallel executor, process-wide resource governor, bounded transactional build coordinator, progress and status, fair multi-worker daemon, filesystem watcher, network polling, targeted passes |
| `ragmonk-storage` | SQLite control plane (`state/control.db`), per-project knowledge stores (`projects/<id>/knowledge.db`) and the storage-neutral `KnowledgeRead` port every retrieval stage reads through |
| `ragmonk-config` | `config.yaml` loading, layering (defaults < user < project < env < CLI), typed validation |
| `ragmonk-core` | errors and exit codes, domain models, home layout, stable IDs, secret filter, path guard, version |
| `ragmonk-update` | native self-update: strict release tags, `update.json` cache, SHA-256 + optional minisign verification, `versions/<ver>` + `current` layout with rollback (ADR 0029) |
| `ragmonk-telemetry` | JSON-lines logging, URL/credential redaction |

Releases are built by `.github/workflows/rust-release.yml` with
`cargo xtask package`, and the container image comes from the root
`Dockerfile` (ADR 0030).

## Quality gates
```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo xtask branding-audit
cargo xtask clean-slate-audit
```

## Test fixtures
`fixtures/` holds the code and document corpora the tests index, and
`fixtures/expected/` the expected outputs that snapshot tests compare
against. Snapshot tests that support it regenerate their expectations with
`RAGMONK_BLESS=1`.

## Storage modes

`storage.mode` is resolved in one place (`ragmonk_service::backend`). In
local mode the SQLite stores are authoritative; in server mode the
OpenSearch/Elasticsearch indexes are, and nothing local is read for the
catalog, builds or knowledge (ADR 0033). Retrieval is written once against
`KnowledgeRead`, so lexical, graph, context and semantic stages behave the
same on both backends (ranking scores are each engine's own).

Design decisions are recorded in [`docs/adr/`](adr).
