# RagMonk

<div align="center">

**Local-first knowledge compiler and retrieval engine for company documents and software repositories.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)
[![Zero Telemetry](https://img.shields.io/badge/telemetry-none-brightgreen)](https://github.com/gzarog/RagMonk)
[![Offline First](https://img.shields.io/badge/offline-first-orange)](https://github.com/gzarog/RagMonk)

</div>

---

## What is RagMonk?

Point RagMonk at your company documents and source code. It builds a **queryable, evidence-backed knowledge base that lives entirely on your machine** — no cloud, no telemetry, no LLM required to get answers.

Ask questions like:

> *"What does the onboarding policy say about remote work?"*
> *"What breaks if `SettlementService` changes?"*
> *"Who calls `processInvoice` and which spec documents describe it?"*

Every answer is traced back to exact files, pages, headings, and lines — not guesses.

---

## Why RagMonk?

| Typical RAG tool | RagMonk |
|---|---|
| Embeddings first, determinism optional | **Deterministic by default** — symbol lookup, call graphs, full-text search, and cross-domain code↔doc linking work with zero LLM calls |
| "Confident" answers with no source | **Evidence-first** — every result carries a confidence tier (`EXACT` / `HIGH` / `MEDIUM` / `HEURISTIC`) and a precise source location |
| Data leaves your machine | **Fully local** — no data leaves your machine, no telemetry, no external AI unless you explicitly opt in |
| Opaque, rebuildable only with support | **Transparent** — everything RagMonk stores is derived state; `ragmonk rebuild` proves source files are the real truth |

---

## Company Documents — First-Class Citizens

RagMonk is built for organizations that need answers from their own document corpus before they ever touch a codebase.

### Supported Formats

| Format | Notes |
|---|---|
| **PDF** | Docling layout model — tables, headings, page numbers extracted |
| **DOCX** | Full heading structure with page provenance |
| **PPTX** | Slide titles and body text with slide number provenance |
| **XLSX / ODS** | Spreadsheet data |
| **HTML** | Web pages and exported wikis |
| **Markdown** | Docs-as-code, ADRs, runbooks |
| **TXT / EML** | Plain text and email files |
| **ODT / ODP** | OpenDocument formats |

### How It Works

A [Docling](https://github.com/docling-project/docling)-backed pipeline converts every document into a normalized **heading → paragraph → table** structure with **page number** and **heading-path provenance** on every unit:

```
docs/onboarding-policy.pdf  →  page 4, §Remote Work Policy > §Equipment Allowance
contracts/msa-2024.docx     →  page 12, §Termination > §Notice Period
```

PDFs are converted to Markdown internally and cached by content hash — **re-indexing an unchanged PDF skips the expensive layout model entirely**.

A corrupt or unsupported document is isolated and recorded as failed rather than aborting the run.

### Cross-Domain Linking: Documents ↔ Code

After indexing, RagMonk automatically connects code entities to the documents that describe them — matching on exact/qualified identifiers, filenames, and HTTP route mentions, each scored on the same confidence ladder as the code graph.

```bash
ragmonk impact SettlementService
# → callers, callees, tests, AND the spec docs that mention it
```

Manage the link graph explicitly:

```bash
ragmonk link list
ragmonk link add --symbol SettlementService --doc contracts/settlement-spec.pdf
ragmonk link remove <ID>
```

Automated linking never overrides an explicit one.

---

## Code Intelligence

Tree-sitter-based parsing extracts a rich entity/relationship graph from your repositories:

- **Entities**: classes, interfaces, structs, enums, functions, methods, properties, fields, imports, inheritance, calls
- **Queries**: `symbol`, `callers`, `callees`, `references`, `impact`

**Fully supported languages**: Python, JavaScript, TypeScript/TSX, Go, Java, Rust, C#

A file in an unsupported language still indexes (without extracted entities); a file that fails to parse is isolated without stopping the run.

```bash
ragmonk symbol SettlementService          # look up a symbol
ragmonk callers SettlementService         # who calls it
ragmonk callees SettlementService         # what it calls
ragmonk impact SettlementService          # blast-radius: callers, docs, tests
```

---

## Search & Retrieval

### `ragmonk explore` — the primary retrieval command

A deterministic query planner picks the right strategies for your question — symbol lookup, graph traversal, full-text, document links, semantic search — and assembles a budgeted, deduplicated evidence package.

```bash
ragmonk explore "what breaks if SettlementService changes?"
ragmonk explore "what does the remote work policy say about equipment?"
```

A high-confidence lexical hit can skip semantic search entirely, avoiding unnecessary embedding inference.

### `ragmonk search` — ranked lexical search

Merges exact/qualified-symbol matches, alias matches, indexed path hits, and FTS5 full-text across both code and documents:

```bash
ragmonk search "SettlementService"
ragmonk search "SettlementService" --explain     # per-stage timing + query kind
ragmonk search "SettlementService" --hybrid      # lexical + semantic, merged and reranked
```

Document hits render as **match-centered snippet blocks** — a real excerpt via SQLite FTS5's `snippet()`, plus the page number (PDF/DOCX/PPTX) or heading path (Markdown/HTML/text).

### `ragmonk impact` — blast-radius analysis

```bash
ragmonk impact SettlementService
# → defining location, callers/callees, linked docs, test files, LOW/MEDIUM/HIGH bucket
```

---

## Installation

Requires **Python 3.12+** already on your `PATH`.

### One-command install

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/gzarog/RagMonk/main/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/gzarog/RagMonk/main/install.ps1 | iex
```

Installs into `~/.ragmonk` (`%LOCALAPPDATA%\RagMonk` on Windows) and links `ragmonk` onto your `PATH`.

```bash
ragmonk version
ragmonk doctor
```

### Install from source

```bash
git clone https://github.com/gzarog/RagMonk.git
cd RagMonk
pip install -e ".[dev]"   # includes test suite and linting tools
```

On first use of document ingestion or semantic search, RagMonk downloads and locally caches small ML models (Docling's layout model for PDFs, a sentence-embedding model). After that, everything runs fully offline.

---

## Quick Start

```bash
ragmonk init                                        # create ~/.ragmonk
ragmonk source add /path/to/docs-or-repo            # register a folder
ragmonk source add /path/to/another-repo            # add more sources
ragmonk index                                        # parse code + documents
ragmonk status --json                               # what got indexed
ragmonk doctor                                      # health check

# Ask questions
ragmonk explore "what does the onboarding policy say about equipment?"
ragmonk explore "what breaks if SettlementService changes?"
ragmonk search "remote work policy"
ragmonk symbol SettlementService
ragmonk callers SettlementService
ragmonk impact SettlementService

# Keep the index current
ragmonk watch                                        # foreground file watcher
ragmonk daemon start                                 # background daemon
```

---

## Admin UI

```bash
ragmonk ui    # opens http://127.0.0.1:8765 in your browser
```

A built-in, **local-first administration interface** — no Node.js, no CDN, works fully offline (HTMX vendored inside the wheel).

From the browser you can:

- Manage sources (add, enable, disable, remove)
- Start and monitor indexing with **live progress** (Server-Sent Events)
- Browse indexed documents and inspect extracted chunks
- Test lexical / semantic / hybrid search
- Explore the code-knowledge graph
- Edit configuration safely (Pydantic-validated; credentials never displayed)
- Control the daemon, run health checks, create and restore backups
- Read logs and view system info

Binds to `127.0.0.1` only, with CSRF protection and host-header validation (DNS-rebinding defense). See [docs/ui.md](docs/ui.md).

---

## MCP Server — Agent Integration

```bash
ragmonk serve --mcp   # stdio MCP server for AI coding agents
```

Exposes the full RagMonk surface to any [Model Context Protocol](https://modelcontextprotocol.io)-speaking agent (Claude Code, Cursor, etc.):

| Tool | What it does |
|---|---|
| `ragmonk_explore` | Primary retrieval — deterministic query planner |
| `ragmonk_search` | Ranked lexical + optional hybrid search |
| `ragmonk_symbol` | Symbol lookup |
| `ragmonk_callers` / `ragmonk_callees` | Call graph traversal |
| `ragmonk_impact` | Blast-radius analysis |
| `ragmonk_documents` | Document listing and chunk inspection |
| `ragmonk_status` | Knowledge base status |
| `ragmonk_ask` | AI-assisted answer (opt-in) |

Register with your agent client automatically:

```bash
ragmonk install-agent --client claude-code    # writes .mcp.json
ragmonk install-agent --client cursor         # writes .cursor/mcp.json
ragmonk install-agent --client vscode         # writes .vscode/mcp.json
ragmonk install-agent --client all            # all of the above
```

Responses are versioned, bounded in size, and every call has a configurable timeout. As a long-lived process, the embedding model, database connections, and ANN index all stay warm across repeated agent calls.

---

## AI-Assisted Answers (Optional)

Everything above works fully offline with no LLM. Two opt-in extras layer on top:

### Local semantic search

```bash
ragmonk config set search.semantic true
# Uses local sentence embeddings — no network after the model is cached
# Results appear as a clearly lower-confidence tier, never mixed into exact matches
```

### AI-generated answers

```bash
ragmonk config set ai.provider ollama
ragmonk config set ai.model llama3.2
ragmonk ask "how are settlement retries handled?"
```

Supported providers: **OpenAI**, **Anthropic**, **Ollama**, any OpenAI-compatible endpoint. Cloud providers require explicitly opting in:

```bash
ragmonk config set privacy.external_ai_allowed true
```

### Subscription providers (beta)

Use an existing AI subscription instead of an API key:

```bash
# ChatGPT via Codex runtime
ragmonk ai login codex
ragmonk config set ai.provider codex
ragmonk ask "Explain the settlement flow and cite the source files"

# GitHub Copilot (pip install "ragmonk[copilot]" first)
ragmonk ai login github_copilot
ragmonk config set ai.provider github_copilot
```

```bash
ragmonk ai providers                  # list providers and capabilities
ragmonk ai status codex               # connection state (no secrets printed)
ragmonk ai models codex               # available models
ragmonk ai logout codex
```

RagMonk never asks for a password, copies browser cookies, stores a token in config, or silently falls back to a billable API key. These adapters are **beta** — see [`docs/providers/`](docs/providers/) for setup, limitations, and tested versions.

---

## Operations

```bash
ragmonk backup [PATH]             # consistent snapshot of the knowledge base
ragmonk restore ARCHIVE           # verify + integrity-check, then atomically swap in
ragmonk rebuild [--source ID]     # wipe and re-index from scratch
ragmonk upgrade                   # apply pending schema migrations (auto-backs up first)
ragmonk update [check|install]    # check for / install a newer RagMonk release
ragmonk uninstall [--keep-data]   # remove the app (and optionally its data)
ragmonk vectors rebuild           # rebuild the semantic-search ANN index from scratch
```

Settings live in `~/.ragmonk/config.yaml` and are always readable/writable via `ragmonk config get|set`. By default: no data leaves your machine, no telemetry is sent, and no external AI provider is called.

---

## Storage Backends: Local vs. Server

By default RagMonk stores everything in a local, embedded stack (SQLite + FTS5 for lexical search, USearch for semantic/ANN search) — no server process, nothing to run, fully offline. For larger or shared deployments, RagMonk can instead store its knowledge base in a self-managed **OpenSearch** or **Elasticsearch** cluster.

### Choosing a mode at `ragmonk init`

```bash
# Local (default) -- identical to plain `ragmonk init`
ragmonk init --storage-mode local

# Server -- OpenSearch
ragmonk init --storage-mode server \
    --storage-engine opensearch \
    --storage-url https://localhost:9200 \
    --storage-index-prefix ragmonk \
    --storage-verify-tls          # or --storage-no-verify-tls for a dev cluster

# Server -- Elasticsearch
ragmonk init --storage-mode server \
    --storage-engine elasticsearch \
    --storage-url https://localhost:9200

# Or answer prompts interactively instead of passing flags
ragmonk init --interactive
```

`ragmonk init` validates the selected engine with its real client before writing anything: the endpoint must be reachable, credentials (if any) must be accepted, the cluster must actually be the selected engine (an OpenSearch cluster never passes as Elasticsearch, or vice versa, and a generic HTTP server answering `200` passes neither), the version must be supported (OpenSearch 2.0+, Elasticsearch 8.0+), and non-destructive permission checks must succeed. No config file is written if any check fails. `--storage-url` must not contain credentials (`https://user:password@host` is rejected) — use the env vars below. Storage mode is fixed at init time for a given `~/.ragmonk` runtime directory; there is no in-place migration between local and server storage.

Install the matching client library first — these are optional extras, never installed by plain `pip install ragmonk`:

```bash
pip install "ragmonk[opensearch]"       # storage.server.engine=opensearch
pip install "ragmonk[elasticsearch]"    # storage.server.engine=elasticsearch
pip install "ragmonk[server]"           # both, if you want to switch engines later
```

### Server credentials

Credentials are **never** written to `config.yaml` — only the connection shape (`url`, `engine`, `index_prefix`, `verify_tls`) is persisted. Set these environment variables instead, at call time, for whichever engine you use:

| Engine | Username | Password | API key |
|---|---|---|---|
| OpenSearch | `RAGMONK_OPENSEARCH_USERNAME` | `RAGMONK_OPENSEARCH_PASSWORD` | `RAGMONK_OPENSEARCH_API_KEY` |
| Elasticsearch | `RAGMONK_ELASTICSEARCH_USERNAME` | `RAGMONK_ELASTICSEARCH_PASSWORD` | `RAGMONK_ELASTICSEARCH_API_KEY` |

An API key, when set, takes precedence over username/password. Both are optional — an unauthenticated dev cluster needs neither.

### Local test clusters (dev-only)

`docker/docker-compose.opensearch.yml` and `docker/docker-compose.elasticsearch.yml` spin up single-node, security-disabled clusters for local testing. **These are a developer convenience only, never a runtime requirement** — RagMonk itself never expects or manages a Docker daemon.

```bash
docker compose -f docker/docker-compose.opensearch.yml up -d
# or
docker compose -f docker/docker-compose.elasticsearch.yml up -d
```

Both expose port `9200`. To run this project's own integration test suites against them (skipped by default — see `CONTRIBUTING.md`):

```bash
OPENSEARCH_URL=http://localhost:9200 \
    pytest -m opensearch_integration

ELASTICSEARCH_URL=http://localhost:9200 \
    pytest -m elasticsearch_integration
```

(Note: these test-suite env vars, `OPENSEARCH_URL`/`ELASTICSEARCH_URL`, are separate from the `RAGMONK_*` credential env vars above — the compose clusters have security disabled and need no credentials at all.)

### Diagnosing and recovering in server mode

- `ragmonk doctor` and `ragmonk status` are backend-aware: in server mode they report cluster reachability and index-level counts from OpenSearch/Elasticsearch itself rather than from local SQLite tables.
- `ragmonk daemon start` fails startup clearly, with an explicit error, if the configured server backend is unreachable — it does not silently fall back to local storage or start in a half-working state.
- `ragmonk rebuild` publishes each source's rebuilt content as a new **generation**, atomically switching reads over only once the rebuild finishes. If a rebuild fails partway through, the **previous generation stays active and searchable** — a failed rebuild degrades to "stale but consistent," never to "partially indexed."

### How server mode stores data

- **Searchable knowledge** (files, code entities, relationships, documents, chunks, embeddings, cross-domain links) is written only to the configured OpenSearch/Elasticsearch cluster — by `ragmonk index`, the daemon, the Admin UI indexer and `ragmonk rebuild` alike. There is no silent local fallback: if the cluster is unreachable, commands fail with an explicit error.
- **Local SQLite remains the control plane** only: scan/diff state, file status, the job queue and retries, content fingerprints and version/embedding bookkeeping. It is never used to answer a search or graph query in server mode.
- **Generations**: every artifact is tagged with its source's write generation, and every read is filtered to the published one. A source's first index and every `ragmonk rebuild` run inside a new generation that becomes visible atomically on success; a failed rebuild (including one where any file fails to index) is aborted and the previous generation keeps being served. Incremental passes update the published generation in place, file by file.
- Switching an existing runtime directory from local to server mode re-indexes each source into the server on its first `ragmonk index`, resetting that source's local control-plane state.

### Known limitations in server mode

- Name-only (unresolved) call edges recorded before the target symbol existed are only found through a resolved entity.

---

## Further Reading

- [`CHANGELOG.md`](CHANGELOG.md) — detailed history of every release
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup, test suite, linting
- [`SECURITY.md`](SECURITY.md) — security policy and vulnerability reporting
- [`docs/ui.md`](docs/ui.md) — Admin UI reference
- [`docs/providers/`](docs/providers/) — subscription AI provider setup and limitations
