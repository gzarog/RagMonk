<div align="center">

# 🧘 RagMonk

### Ask your company documents — and your code — anything.
### Get answers with exact sources. Entirely on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)
[![Zero Telemetry](https://img.shields.io/badge/telemetry-none-brightgreen)](https://github.com/gzarog/RagMonk)
[![Offline First](https://img.shields.io/badge/offline-first-orange)](https://github.com/gzarog/RagMonk)

**Documents** · **Code** · **Documents ↔ Code** · **Local or OpenSearch/Elasticsearch**

</div>

---

## 📑 Agenda

| # | Slide |
|---|---|
| 1 | [The problem](#1--the-problem) |
| 2 | [What RagMonk is](#2--what-ragmonk-is) |
| **Part I — Documents** | |
| 3 | [Your documents, first-class](#3--your-documents-first-class) |
| 4 | [How a document becomes knowledge](#4--how-a-document-becomes-knowledge) |
| 5 | [Asking your documents](#5--asking-your-documents) |
| **Part II — Code** | |
| 6 | [Code intelligence](#6--code-intelligence) |
| 7 | [Documents ↔ Code linking](#7--documents--code-linking) |
| 8 | [One question, every strategy: `explore`](#8--one-question-every-strategy-explore) |
| **Part III — Using it** | |
| 9 | [Install in one command](#9--install-in-one-command) |
| 10 | [Five-minute quick start](#10--five-minute-quick-start) |
| 11 | [Admin UI](#11--admin-ui) |
| 12 | [Plug into AI agents (MCP)](#12--plug-into-ai-agents-mcp) |
| 13 | [Optional AI layer](#13--optional-ai-layer) |
| 14 | [Scale out: server storage](#14--scale-out-server-storage) |
| 15 | [Operate with confidence](#15--operate-with-confidence) |
| 16 | [Summary & further reading](#16--summary--further-reading) |

---

## 1 · The problem

> *"What does the onboarding policy say about remote equipment?"*
> *"Which spec describes the settlement retry rules — and what code implements them?"*
> *"What breaks if `SettlementService` changes?"*

- Knowledge is split across **PDFs, Word, slides, spreadsheets, wikis** — and **repositories**
- Typical RAG tools send that data to the cloud and return **confident answers without sources**
- Nobody can tell which paragraph, page, or line an answer actually came from

---

## 2 · What RagMonk is

A **local-first knowledge compiler and retrieval engine**: point it at folders, it builds an evidence-backed knowledge base on your machine.

| Typical RAG tool | RagMonk |
|---|---|
| Embeddings first, determinism optional | **Deterministic by default** — full-text, symbol lookup, call graphs and doc↔code links need **zero LLM calls** |
| Answers without a source | **Evidence-first** — every hit has a confidence tier (`EXACT` / `HIGH` / `MEDIUM` / `HEURISTIC`) and an exact location |
| Data leaves your machine | **Fully local** — no telemetry, no external AI unless you explicitly opt in |
| Opaque storage | **Derived state only** — `ragmonk rebuild` regenerates everything from your source files |

---

# Part I — Documents

---

## 3 · Your documents, first-class

RagMonk is built for teams that need answers from their **document corpus** first.

| Format | What you get |
|---|---|
| **PDF** | Layout model: headings, tables, page numbers · OCR for scanned pages (`documents.ocr: auto`) |
| **DOCX / ODT** | Heading structure with page provenance |
| **PPTX / ODP** | Slide titles and body text, slide-number provenance |
| **XLSX / ODS / CSV** | Tables rendered and chunked row-by-row |
| **HTML / Markdown** | Wikis, docs-as-code, ADRs, runbooks |
| **TXT / EPUB** | Plain text and e-books |
| **PNG / JPG / TIFF** | Opt-in OCR (`documents.image_ocr: true`) |

- A corrupt or unsupported file is **isolated and recorded as failed** — the run continues
- Legacy `.doc` / `.ppt` / `.xls` / `.rtf` are detected but not converted — save them in a modern format

---

## 4 · How a document becomes knowledge

```
 file ──► Docling conversion ──► heading → paragraph → table tree ──► token-aware chunks ──► index
            (cached by hash)        + page & heading-path provenance       (tables split at row boundaries)
```

Every chunk knows **where it came from**:

```
docs/onboarding-policy.pdf  →  page 4 · Remote Work Policy › Equipment Allowance
contracts/msa-2024.docx     →  page 12 · Termination › Notice Period
```

- **PDF conversions are cached by content hash** — re-indexing an unchanged PDF skips the layout model
- **Incremental**: only new or changed files are reprocessed; moved files are detected, not re-parsed
- Chunk sizes follow the **embedding model's real tokenizer** (`documents.chunking.max_tokens: auto`)

---

## 5 · Asking your documents

```bash
ragmonk search "remote work equipment allowance"
```

- Hits render as **match-centred snippets** with page number or heading path
- `--table` · `--json` · `--limit N` · `--explain` (per-stage timings) · `--hybrid` (lexical + semantic)

```bash
ragmonk docs                   # list every indexed document
ragmonk docs --source <id>     # just one source
```

A matched paragraph or table row is expanded with its **surrounding context** (nearest heading, neighbouring rows) so the evidence reads on its own.

---

# Part II — Code

---

## 6 · Code intelligence

Tree-sitter parsing builds an **entity / relationship graph** of your repositories.

| | |
|---|---|
| **Languages (full extraction)** | Python · JavaScript · TypeScript/TSX · Go · Java · Rust · C# |
| **Entities** | classes, interfaces, structs, enums, functions, methods, properties, fields |
| **Relationships** | imports, inheritance, calls, references |
| **Frameworks** | HTTP routes from Python decorators and C# route attributes |

```bash
ragmonk symbol     SettlementService    # where is it defined?
ragmonk callers    SettlementService    # who calls it?
ragmonk callees    SettlementService    # what does it call?
ragmonk references SettlementService    # every edge touching it
ragmonk impact     SettlementService    # blast radius: callers, docs, tests → LOW / MEDIUM / HIGH
```

Other languages (Kotlin, C/C++, Ruby, PHP, SQL, shell…) are still indexed for full-text search, without extracted entities.

---

## 7 · Documents ↔ Code linking

After each pass RagMonk **connects code entities to the documents that describe them** — exact/qualified identifiers, filenames and HTTP routes, each scored on the same confidence ladder.

```bash
ragmonk impact SettlementService
# → callers, callees, tests … AND the spec documents that mention it
```

Curate links by hand — **an explicit link is never overridden by automation**:

```bash
ragmonk link list
ragmonk link add SettlementService contracts/settlement-spec.pdf   # [--section ID]
ragmonk link remove <ID>
```

---

## 8 · One question, every strategy: `explore`

```bash
ragmonk explore "what does the remote work policy say about equipment?"
ragmonk explore "what breaks if SettlementService changes?"
```

A **deterministic query planner** chooses the strategies — symbol lookup, graph traversal, full-text, document links, semantic — and returns a **budgeted, deduplicated evidence package**.

- A strong lexical hit **skips semantic search** entirely
- The same engine powers the CLI, the Admin UI and the MCP server

---

# Part III — Using it

---

## 9 · Install in one command

Requires **Python 3.12+** on your `PATH`.

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/gzarog/RagMonk/main/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/gzarog/RagMonk/main/install.ps1 | iex
```

- Installs into `~/.ragmonk` (`%LOCALAPPDATA%\RagMonk` on Windows) and puts `ragmonk` on your `PATH`
- **Always includes the server-mode clients** (`ragmonk[server]`: `opensearch-py` + `elasticsearch`) — server storage works with no extra `pip install`
- `ragmonk update install` re-runs the same installer

From source:

```bash
git clone https://github.com/gzarog/RagMonk.git && cd RagMonk
pip install -e ".[dev]"
```

First document ingestion / semantic search downloads small ML models once; afterwards everything runs **offline**.

---

## 10 · Five-minute quick start

```bash
ragmonk init                                # create ~/.ragmonk
ragmonk source add ~/company-docs           # 1. your documents
ragmonk source add ~/code/payments          # 2. your code
ragmonk index                               # parse & index everything
ragmonk status                              # what got indexed
ragmonk doctor                              # health check

ragmonk explore "what does the onboarding policy say about equipment?"
ragmonk impact SettlementService

ragmonk daemon start                        # keep the index fresh in the background
```

- `ragmonk index` keeps going when one source fails, reports it, and exits non-zero at the end
- **Last Scan** = last completed filesystem scan; a later failure shows up as **Last Error** instead

---

## 11 · Admin UI

```bash
ragmonk ui        # → http://127.0.0.1:8765
```

- Manage sources · start indexing with **live progress**
- Browse documents and inspect their chunks
- Try lexical / semantic / hybrid search · explore the code graph
- Edit config safely · control the daemon · backups · logs

Local-only: binds to `127.0.0.1`, CSRF protection, host-header validation, no CDN. → [docs/ui.md](docs/ui.md)

---

## 12 · Plug into AI agents (MCP)

```bash
ragmonk install-agent --client claude-code   # or codex · cursor · vscode · all
ragmonk serve --mcp                          # stdio MCP server
```

| Tool | Purpose |
|---|---|
| `ragmonk_explore` | Primary retrieval (query planner) |
| `ragmonk_search` | Lexical / hybrid search |
| `ragmonk_documents` | List documents, inspect chunks |
| `ragmonk_symbol` · `ragmonk_callers` · `ragmonk_callees` | Code graph |
| `ragmonk_impact` | Blast radius |
| `ragmonk_status` | Knowledge-base status |
| `ragmonk_ask` | AI answer (opt-in) |

Bounded, versioned responses; models and indexes stay warm between calls.

---

## 13 · Optional AI layer

Everything so far works **without any LLM**. Two opt-ins:

**Local semantic search** — sentence embeddings on your machine, shown as a lower-confidence tier:

```bash
ragmonk config set search.semantic true
```

**AI-written answers grounded in the evidence**:

```bash
ragmonk config set ai.provider ollama && ragmonk config set ai.model llama3.2
ragmonk ask "how are settlement retries handled?"
```

| Providers | Notes |
|---|---|
| Ollama · OpenAI-compatible | Local or self-hosted |
| OpenAI · Anthropic | Require `privacy.external_ai_allowed: true` |
| Codex · GitHub Copilot (beta) | Use an existing subscription: `ragmonk ai login <provider>` → [docs/providers/](docs/providers/) |

RagMonk never stores tokens in config, never copies browser cookies, never silently falls back to a paid API.

---

## 14 · Scale out: server storage

| | **Local (default)** | **Server** |
|---|---|---|
| Search store | SQLite + FTS5 + USearch | OpenSearch 2+ / Elasticsearch 8+ |
| Runs | Nothing to run | Your existing cluster |
| Best for | One machine | Large or shared corpora |

```bash
ragmonk init --storage-mode server --storage-engine opensearch \
    --storage-url https://search.internal:9200
```

- `init` **validates the real cluster** first (reachable, right engine, supported version, permissions)
- **Credentials only via env vars** — `RAGMONK_OPENSEARCH_{USERNAME,PASSWORD,API_KEY}` / `RAGMONK_ELASTICSEARCH_…`; never in `config.yaml`, redacted in every output
- **Generations**: first index and every rebuild publish atomically; a failed rebuild keeps serving the previous one
- Local SQLite stays the **control plane** only (scan state, queue); no silent local fallback

Dev clusters: `docker compose -f docker/docker-compose.opensearch.yml up -d` (or `…elasticsearch.yml`).

---

## 15 · Operate with confidence

```bash
ragmonk backup [PATH]              # consistent snapshot
ragmonk restore ARCHIVE            # verify, then atomic swap
ragmonk rebuild [--source ID]      # re-index from scratch
ragmonk upgrade                    # schema migrations (auto-backup first)
ragmonk update check|install       # new releases
ragmonk vectors rebuild            # rebuild the ANN index
ragmonk uninstall [--keep-data]
```

- Unreachable source? Marked **OFFLINE** — its knowledge is **never deleted** by mistake
- Config in `~/.ragmonk/config.yaml`, via `ragmonk config get|set`
- Defaults: **no data leaves your machine, no telemetry, no external AI**

---

## 16 · Summary & further reading

<div align="center">

**📄 Documents** → structured, cited, searchable<br>
**💻 Code** → symbols, call graphs, blast radius<br>
**🔗 Linked** → specs ↔ implementation<br>
**🔒 Local** → your data stays yours

</div>

- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, tests, linting
- [`SECURITY.md`](SECURITY.md) — security policy
- [`docs/ui.md`](docs/ui.md) — Admin UI reference
- [`docs/providers/`](docs/providers/) — subscription AI providers
