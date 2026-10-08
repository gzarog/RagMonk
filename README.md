<div align="center">

# 🧘 RagMonk

### Ask your company documents, and your code, anything.
### Answers with exact sources. Entirely on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Native binary](https://img.shields.io/badge/native-Rust-orange)](docs/architecture.md)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)
[![Zero Telemetry](https://img.shields.io/badge/telemetry-none-brightgreen)](https://github.com/gzarog/RagMonk)

**[Install](#-install)** · **[Quick start](#-quick-start-5-minutes)** · **[Features](#-what-you-get)** · **[Commands](#-command-cheat-sheet)** · **[Status](#-is-it-working-ragmonk-status)** · **[Configuration](#%EF%B8%8F-configuration)** · **[FAQ](#-faq--troubleshooting)**

</div>

---

<table>
<tr>
<td width="33%" valign="top">

### 📄 Documents
PDF, Word, PowerPoint, Excel, CSV, HTML, Markdown, OpenDocument, EPUB, plus OCR for scanned files. Headings, tables and page numbers are preserved.

</td>
<td width="33%" valign="top">

### 💻 Code
Python, JavaScript, TypeScript, Go, Java, Rust, C#. Symbols, callers, call graphs, HTTP routes and impact analysis.

</td>
<td width="33%" valign="top">

### 🔒 Private
No telemetry, no cloud, no external AI unless you opt in. Your data stays in `~/.ragmonk`.

</td>
</tr>
</table>

---

## 🤔 Why RagMonk?

> *"What does the onboarding policy say about remote equipment?"*
> *"Which spec describes settlement retries, and what code implements them?"*

Knowledge is scattered across **PDFs, Word files, slides, spreadsheets, wikis** and **repositories**. Most RAG tools send that data to the cloud and answer **without sources**.

RagMonk is a **local-first knowledge engine**. Point it at folders and you get an evidence-backed knowledge base:

| | |
|---|---|
| ✅ **Evidence-first** | Every answer cites a file plus its page, heading or line |
| ⚙️ **Deterministic** | Full-text search, symbols, call graphs and document↔code links work with **no LLM at all** |
| 🔗 **Linked** | Specs and docs are automatically linked to the code they mention |
| 🧩 **Everywhere** | CLI, web Admin UI, and AI agents via MCP (Claude Code, Codex, Cursor, VS Code) |

---

## 📦 Install

| Platform | Command |
|---|---|
| **macOS / Linux** | `curl -fsSL https://raw.githubusercontent.com/gzarog/RagMonk/main/install.sh \| sh` |
| **Windows (PowerShell)** | `irm https://raw.githubusercontent.com/gzarog/RagMonk/main/install.ps1 \| iex` |
| **Docker** | `docker pull ghcr.io/gzarog/ragmonk` (see [Container](docs/adr/0030-release-and-container.md)) |

**Requirements:** none. A single native binary for Linux x86_64, macOS (Apple silicon and Intel) and Windows x86_64, with the embedding, reranker and OCR models bundled, so everything runs offline from the first start. Update in place with `ragmonk update install`.

Check the install with `ragmonk version` and `ragmonk doctor`.

---

## 🚀 Quick start (5 minutes)

```bash
ragmonk init                              # 1. create the runtime folder (~/.ragmonk)
ragmonk source add ~/company-docs         # 2. register folders to index
ragmonk source add ~/code/payments
ragmonk index                             # 3. build the knowledge base
ragmonk status                            # 4. check progress and errors
ragmonk explore "what does the onboarding policy say about equipment?"   # 5. ask
```

Example of what you get back:

```
docs/onboarding-policy.pdf  →  page 4 · Remote Work Policy › Equipment Allowance
contracts/msa-2024.docx     →  page 12 · Termination › Notice Period
src/payments/settlement.py  →  line 88 · SettlementService.retry()
```

> 💡 **Keep it fresh automatically:** run `ragmonk daemon start`, and changed files are re-indexed in the background.

---

## ✨ What you get

<details open>
<summary><b>📄 Documents</b></summary>

| Format | What is extracted |
|---|---|
| **PDF** | Layout model: headings, tables, page numbers · OCR for scanned pages (`documents.ocr: auto`) |
| **DOCX / ODT** | Heading structure with page provenance |
| **PPTX / ODP** | Slide titles and body text, slide numbers |
| **XLSX / ODS / CSV** | Tables, chunked row by row |
| **HTML / Markdown** | Wikis, docs-as-code, ADRs, runbooks |
| **TXT / EPUB** | Plain text and e-books |
| **PNG / JPG / TIFF** | Opt-in OCR (`documents.image_ocr: true`) |

- A corrupt file is isolated and marked as failed. The rest of the run continues.
- Older binary `.doc`, `.ppt`, `.xls` files and `.rtf` files are detected but not converted.
- Incremental: only changed files are reprocessed, and PDF conversions are cached by content.

</details>

<details open>
<summary><b>💻 Code</b></summary>

Full extraction for **Python, JavaScript, TypeScript/TSX, Go, Java, Rust and C#**, including HTTP routes from Python decorators and C# attributes. Other languages are still indexed for full-text search.

```bash
ragmonk symbol SettlementService      # where is it defined?
ragmonk callers SettlementService     # who calls it?
ragmonk impact SettlementService      # callers, tests, and the specs that mention it
```

</details>

<details open>
<summary><b>🔎 One question, one command</b></summary>

```bash
ragmonk explore "what breaks if SettlementService changes?"
```

A deterministic planner combines symbol lookup, the call graph, full-text search, document links and (optionally) semantic search into **one cited evidence package**.

</details>

<details>
<summary><b>🖥️ Admin UI</b></summary>

```bash
ragmonk ui          # → http://127.0.0.1:8765
```

Manage sources, run indexing with live progress, browse document chunks, test search, explore the code graph, edit settings, and control the daemon, backups and logs. It listens only on `127.0.0.1`, with CSRF protection. See [docs/ui.md](docs/ui.md).

</details>

<details>
<summary><b>🤖 AI agents (MCP)</b></summary>

```bash
ragmonk serve --mcp   # register this command as a stdio MCP server in your client
```

Tools exposed: `ragmonk_explore`, `ragmonk_search`, `ragmonk_documents`, `ragmonk_symbol`, `ragmonk_callers`, `ragmonk_callees`, `ragmonk_impact`, `ragmonk_status`, `ragmonk_ask`.

</details>

<details>
<summary><b>🧠 Optional AI answers and semantic search</b></summary>

```bash
ragmonk config set search.semantic true      # local semantic search (no cloud)
ragmonk config set ai.provider ollama
ragmonk config set ai.model llama3.2
ragmonk ask "how are settlement retries handled?"
```

| Provider | Notes |
|---|---|
| **Ollama**, OpenAI-compatible | Local / self-hosted |
| **OpenAI**, **Anthropic** | Require `privacy.external_ai_allowed: true` |
| **Codex**, **GitHub Copilot** (beta) | `ragmonk ai login <provider>`, see [docs/providers](docs/providers/) |

</details>

<details>
<summary><b>🏢 Large or shared corpora: OpenSearch / Elasticsearch</b></summary>

```bash
ragmonk init --storage-mode server --storage-engine opensearch \
    --storage-url https://search.internal:9200
```

- `init` checks the real cluster first: reachable, right engine, OpenSearch 2+ / Elasticsearch 8+, permissions.
- Credentials come **only** from environment variables (`RAGMONK_OPENSEARCH_USERNAME`, `…_PASSWORD`, `…_API_KEY`, and the same names with `ELASTICSEARCH`). They are never stored in `config.yaml`.
- Rebuilds publish atomically. A failed rebuild keeps serving the previous index.
- Local dev cluster: `docker compose -f docker/docker-compose.opensearch.yml up -d`

</details>

---

## 📋 Command cheat sheet

| Task | Command |
|---|---|
| Set up | `ragmonk init` |
| Add / list / remove a folder | `ragmonk source add PATH [--include GLOB] [--exclude GLOB]` · `source list` · `source remove ID` |
| Pause / resume a folder | `ragmonk source disable ID` · `source enable ID` |
| Index now | `ragmonk index [--source ID]` |
| Index automatically | `ragmonk daemon start` · `daemon status` · `daemon stop` |
| Is it working? | `ragmonk status [--watch] [--errors] [--verbose] [--json]` |
| Search | `ragmonk search "text" [--hybrid] [--table] [--json]` |
| Ask across everything | `ragmonk explore "question"` · `ragmonk ask "question"` (AI) |
| Code navigation | `ragmonk symbol` · `callers` · `callees` · `references` · `impact` `NAME` |
| List documents | `ragmonk docs [--source ID]` |
| Pin a doc↔code link | `ragmonk link add ENTITY DOCUMENT` · `link list` · `link remove ID` |
| Web UI | `ragmonk ui` |
| Health check | `ragmonk doctor` · `ragmonk health` |
| Settings | `ragmonk config show` · `config get KEY` · `config set KEY VALUE` |
| Backup / restore | `ragmonk backup [PATH]` · `ragmonk restore ARCHIVE` |
| Rebuild from scratch | `ragmonk rebuild [--source ID] [--fresh]` |
| Update RagMonk | `ragmonk update check` · `update install` |
| Uninstall | `ragmonk uninstall [--keep-data]` |

Every command supports `--help`.

---

## 📊 Is it working? `ragmonk status`

`ragmonk status` tells you whether indexing is **running, progressing, stalled or failing**, and what failed. Example while indexing:

```
╭──────────────── Indexer ────────────────╮
│ State: running                          │
│ PID: 44884  Operation: index            │
│ Source: src_8322e (17/150)  Stage: processing  Last activity: 2s ago │
╰─────────────────────────────────────────╯
Health: degraded (2 problem(s))
Jobs: queued=720 processing=1 retry=23 failed=7
```

| Flag | What it does |
|---|---|
| `--watch` | Live view with deltas and files/sec (Ctrl+C to exit) |
| `--errors` | Only the sources and errors that need attention |
| `--verbose` | Lock owner, timings, next retry, last file error per source |
| `--json` | Full machine-readable status (same data as the MCP tool and Admin UI) |

**Reading the source table**

| Column | Meaning |
|---|---|
| **Access** | Can the folder be reached? `online` · `offline` · `disabled` |
| **Index State** | `indexing` · `waiting` · `retrying` · `errors` · `completed` · `idle` · `offline` · `stalled` |
| **Last Activity** | Last progress heartbeat for the source being indexed, otherwise its last scan |

**Health:** `healthy` · `degraded` (some files failed or are retrying, or a source is offline) · `failed` (backend unreachable, run stalled or crashed, or all sources offline). The `problems` list always says why.

> ⏱️ **"stalled"** means indexing is still running on that source but has reported no progress for **120 seconds** (`indexing.status_stall_threshold_seconds`). A single very large PDF can legitimately take longer than that. If this happens often, raise the threshold.

---

## ⚙️ Configuration

Settings live in `~/.ragmonk/config.yaml` (Windows: `%LOCALAPPDATA%\RagMonk`); unknown keys are rejected. Change them with `ragmonk config set KEY VALUE`, or override any of them with an environment variable, e.g. `RAGMONK_DOCUMENTS__OCR=off`. Set `RAGMONK_HOME` to move the whole runtime folder.

| Setting | Default | What it does |
|---|---|---|
| `documents.ocr` | `auto` | OCR scanned PDFs: `off` · `auto` · `always` |
| `documents.image_ocr` | `false` | OCR plain images (PNG/JPG/TIFF) |
| `documents.max_pages` | `1000` | Skip larger PDFs |
| `documents.pdf_mode` | `accurate` | `fast` = text layer only, much faster, no layout/tables |
| `documents.email_attachments` | `true` | Index supported `.eml` attachments as child documents |
| `search.semantic` | `false` | Local embedding-based semantic search |
| `indexing.watch` | `true` | Daemon watches folders for changes |
| `indexing.max_file_size_mb` | `100` | Skip larger files |
| `indexing.lock_timeout_seconds` | `30` | Max wait when another process is indexing |
| `indexing.status_stall_threshold_seconds` | `120` | When `status` reports "stalled" |
| `ai.provider` / `ai.model` | *(none)* | Optional AI answers for `ragmonk ask` |
| `privacy.external_ai_allowed` | `false` | Required for cloud AI providers |

<details>
<summary><b>⚡ Faster indexing tips</b></summary>

| Situation | Try |
|---|---|
| Many digital (non-scanned) PDFs, speed matters more than layout | `ragmonk config set documents.pdf_mode fast`: 81s → 1.6s on a 24-PDF test set |
| Big first index | Let it finish once. After that only changed files are reprocessed |

Changing `pdf_mode` reprocesses PDFs on the next `ragmonk index`.

</details>

---

## ❓ FAQ & troubleshooting

<details>
<summary><b>Does any of my data leave my machine?</b></summary>

No. There is no telemetry. Indexes are stored locally (or in *your* OpenSearch/Elasticsearch). Cloud AI providers only work if you explicitly set `privacy.external_ai_allowed: true`.
</details>

<details>
<summary><b>A folder shows <code>offline</code></b></summary>

The folder (for example a network share) could not be reached. Its existing knowledge is **kept, never deleted**. Reconnect it and run `ragmonk index`.
</details>

<details>
<summary><b>A source shows <code>errors</code> or <code>retrying</code></b></summary>

Run `ragmonk status --errors` to see the failing files and why. Files that fail temporarily are retried automatically with backoff. Files that keep failing (corrupt or password-protected) are recorded as failed, and the rest of the run continues.
</details>

<details>
<summary><b>"Another RagMonk process holds index.lock"</b></summary>

Another index run, the daemon or the Admin UI is indexing right now. The error names the PID and the operation. Wait for it, or stop it with `ragmonk daemon stop`. **Do not delete the lock file.** It is released automatically when its owner exits or crashes.
</details>

<details>
<summary><b>The first index is slow</b></summary>

The first run downloads the document models once, and every file is new. Later runs only process changes. See the *Faster indexing tips* above, and watch progress with `ragmonk status --watch`.
</details>

<details>
<summary><b>Something looks broken</b></summary>

Run `ragmonk doctor`. It checks the runtime folder, sources, databases, the index lock, the storage backend and AI providers. If the index itself is damaged: `ragmonk rebuild --fresh`. Your previous index is restored if the rebuild fails.
</details>

---

<div align="center">

**📄 Documents** · **💻 Code** · **🔗 Linked** · **🔒 Local**

📘 [Full reference](docs/reference.md) · 🖥️ [Admin UI guide](docs/ui.md) · 📝 [Releases](https://github.com/gzarog/RagMonk/releases) · 🤝 [Contributing](CONTRIBUTING.md) · 🔐 [Security](SECURITY.md)

MIT licensed

</div>
