# RAGpilot

A local-first knowledge compiler and retrieval engine for software repositories and organizational documents.

## Presentation

RAGpilot turns a folder of source code and documents into a queryable, evidence-backed knowledge base that lives entirely on your machine. Point it at a repository and a docs folder, index them, and then ask questions like "what breaks if `SettlementService` changes?" or "who calls `bark_loudly`?" and get answers traced back to exact files, lines, and pages — not guesses.

It has no web UI. You use it three ways:

- **CLI** — for humans, scripts, and CI (every command supports `--json`)
- **MCP server** (`ragpilot serve --mcp`) — for AI coding agents (Claude Code, Cursor, etc.) that speak the [Model Context Protocol](https://modelcontextprotocol.io)
- **A daemon** (`ragpilot watch` / `ragpilot daemon start`) that keeps the knowledge base current as files change

Two things make it different from a typical embeddings-only RAG tool:

- **Deterministic by default.** Symbol lookup, call graphs, full-text search, and cross-domain code↔doc linking all work with zero network access and zero LLM calls. Semantic (embedding-based) search and LLM-generated answers are opt-in extras layered on top, not the foundation.
- **Evidence-first.** Every result — a caller, a linked document, a search hit — carries a confidence tier (`EXACT`/`HIGH`/`MEDIUM`/`HEURISTIC`) and a precise source location, so you can tell a compiler-grade fact from an inferred guess.

Source files are always the source of truth; everything RAGpilot stores is derived, rebuildable state (`ragpilot rebuild` proves it). Every retrieval path is indexed: exact lookups use SQLite B-tree indexes, lexical search uses FTS5, semantic search uses a persistent USearch HNSW index kept warm in memory, and metadata for every hit is resolved in one batched query — never a full-corpus scan.

## Installation

Requires **Python 3.12+** already on your `PATH` — RAGpilot isn't published on PyPI yet, and these scripts don't install Python itself.

### Install the CLI

One command finds your Python, creates an isolated virtual environment, and installs `ragpilot`:

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/gzarog/Ragpilotv2/main/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/gzarog/Ragpilotv2/main/install.ps1 | iex
```

It installs into `~/.ragpilot` (`%LOCALAPPDATA%\RAGpilot` on Windows) and links `ragpilot` onto a per-user bin directory. If that directory isn't already on your `PATH`, the script prints the one-line fix (on Windows it adds it to your user `PATH` automatically — open a new terminal afterward).

Verify it:

```bash
ragpilot version
ragpilot doctor
```

### Install from source

For development, or to track an unreleased change:

```bash
git clone https://github.com/gzarog/Ragpilotv2.git
cd Ragpilotv2
pip install -e .
```

For running the test suite and linting, install the `dev` extra instead:

```bash
pip install -e ".[dev]"
```

No system dependencies are required beyond Python 3.12+. On first use of a document source or semantic search, RAGpilot downloads and locally caches small ML models (Docling's layout model for PDFs, a sentence-embedding model) — after that, everything runs offline.

## Usage

```bash
ragpilot init                                    # create ~/.ragpilot (or %LOCALAPPDATA%\RAGpilot on Windows)
ragpilot source add /path/to/a/repo/or/docs       # register a folder to index
ragpilot index                                    # parse code + documents, build the knowledge graph
ragpilot status --json                            # what got indexed
ragpilot doctor                                   # health check

ragpilot search "SettlementService"                # lexical search across code + docs
ragpilot search "SettlementService" --explain       # per-stage timing + classified query kind/confidence
ragpilot symbol SettlementService                  # look up a symbol
ragpilot callers SettlementService                  # who calls it
ragpilot impact SettlementService                   # blast-radius analysis: callers, docs, tests
ragpilot explore "what breaks if SettlementService changes?"   # the primary retrieval command

ragpilot serve --mcp                              # expose everything above to an MCP-speaking agent
ragpilot watch                                     # keep indexing as files change (foreground)
ragpilot daemon start                              # keep indexing as files change (background)
ragpilot backup                                    # snapshot the knowledge base
```

Turning on semantic search and AI-assisted answers (both opt-in, off by default):

```bash
ragpilot config set search.semantic true
ragpilot config set ai.provider ollama
ragpilot config set ai.model llama3.2
ragpilot ask "how are settlement retries handled?"
```

Settings live in `<runtime dir>/config.yaml` (`~/.ragpilot` on Linux/macOS, `%LOCALAPPDATA%\RAGpilot` on Windows) and are always readable/writable via `ragpilot config get|set`. By default: no data leaves your machine, no telemetry is sent, and no external AI provider is called.

## Functionalities

### Code intelligence
Tree-sitter-based parsing extracts classes, interfaces, structs, enums, functions, methods, properties, fields, imports, inheritance, and calls into a normalized entity/relationship graph, queryable via `ragpilot symbol|callers|callees|references`. Fully supported languages: **Python, JavaScript, TypeScript/TSX, Go, Java, Rust, C#**. A recognized file in an unsupported language still indexes (just without extracted entities); a file that fails to parse is isolated and recorded as failed without stopping the rest of the run.

### Document ingestion
A [Docling](https://github.com/docling-project/docling)-backed pipeline converts **PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT, and EML** files into a normalized heading/paragraph/table structure with page and heading-path provenance on every unit, exposed via `ragpilot docs`. PDFs are internally converted to Markdown, cached by content hash, and reparsed from that Markdown -- so re-indexing an unchanged PDF skips Docling's expensive layout/table-structure model entirely. A corrupt or unsupported document is isolated and recorded as failed rather than aborting the run.

### Cross-domain linking
After indexing, RAGpilot connects code entities to the documents that describe them — matching on exact/qualified identifiers, filenames, and HTTP route mentions, each scored on the same confidence ladder as the code graph. `ragpilot link add|remove|list` lets you inspect the link graph and pin or remove a mapping by hand; automated linking never overrides an explicit one.

### Search & retrieval
- `ragpilot search QUERY` — ranked lexical search merging exact/qualified-symbol matches, alias matches, indexed path hits, and FTS5 full-text/document title/heading matches, all backed by indexed lookups (no full-corpus scans). Add `--explain` for a per-stage timing breakdown and the classified query kind/confidence, or `--hybrid` for one merged, reranked view of lexical and semantic results (semantic score never outranks a lexical match). Document hits render as match-centered snippet blocks by default (`--snippets` to force it explicitly) — a real, match-centered excerpt via SQLite FTS5's own `snippet()`, plus the page number (PDF/DOCX/PPTX) or heading path (Markdown/HTML/text) it came from; code/symbol hits are unaffected and always show their plain title/path/tier row. `--table` reverts to that plain table for every hit. The default output mode, and what one document hit falls back to when it has no real match snippet (an exact-title hit with no FTS row), are both driven by `search.output.fallback` in `config.yaml` — an ordered list of `snippets`/`json`/`files`/`table`, `["snippets", "json", "files"]` out of the box (`ragpilot config set search.output.fallback json,files`).
- `ragpilot explore "QUERY"` — the primary retrieval command: a deterministic query planner picks the right strategies (symbol lookup, graph traversal, full-text, document links, semantic search) for the question and assembles a budgeted, deduplicated evidence package. A high-confidence lexical hit can skip semantic search entirely (`search.lazy_semantic`), avoiding unnecessary embedding inference.
- `ragpilot impact SYMBOL` — a symbol's defining location, callers/callees, linked documents, a naming-convention "tests" heuristic, and a LOW/MEDIUM/HIGH blast-radius bucket.
- `ragpilot vectors rebuild [--source ID]` — rebuilds the semantic-search ANN index from scratch; `ragpilot doctor` reports which backend is active and how many vectors it holds. The index rebuilds itself automatically once enough vectors have been deleted/tombstoned.
- Query-result and query-embedding caches speed up repeated searches in any long-lived process, and the on-disk USearch index is loaded once and kept warm in memory rather than reread on every query.

### MCP server (agent integration)
`ragpilot serve --mcp` starts a stdio MCP server exposing `ragpilot_explore`, `ragpilot_search`, `ragpilot_symbol`, `ragpilot_callers`, `ragpilot_callees`, `ragpilot_impact`, `ragpilot_documents`, `ragpilot_status`, and `ragpilot_ask` — each a thin wrapper over the same functions backing the CLI, so an agent sees exactly what you'd see at the terminal. Responses are versioned, bounded in size, and every call has a configurable timeout. As a long-lived process, it's also where the embedding model, database connections, and the ANN index all stay warm across repeated calls. `ragpilot install-agent [--write PATH]` prints the config snippet needed to register RAGpilot with a client like Claude Code — it only prints/writes, it never edits a client's config file on its own. Add `--client NAME` (repeatable, or `--client all`) to instead register automatically with that client's real config file at its documented location — `claude-code` (`.mcp.json`), `cursor` (`.cursor/mcp.json`), `vscode` (`.vscode/mcp.json`), or `codex` (`~/.codex/config.toml`). Only the `ragpilot` entry in that file is ever added or updated; everything else already there is left untouched, and re-running it is always safe (a matching entry is left alone, reported as already configured).

### Continuous indexing
`ragpilot watch` (foreground) or `ragpilot daemon start|stop|restart|status` (background) watches every enabled source — local roots via native OS filesystem events, network/UNC roots by polling — and keeps the knowledge base current, plus a periodic full reconciliation as a safety net. A source that goes temporarily unreachable (an unmounted network share, a disconnected drive) is flagged `offline` rather than having its knowledge mistakenly deleted, and reconciles for real once it's back.

### Operations
- `ragpilot backup [PATH]` — an online, consistent snapshot (SQLite's backup API, not a raw file copy) of the knowledge base into one archive. Never includes your original source files.
- `ragpilot restore ARCHIVE` — verifies and integrity-checks a backup before atomically swapping it into place; a bad archive is refused before anything live is touched.
- `ragpilot rebuild [--source ID]` — wipes and re-indexes a source from scratch, proving source files are the real truth.
- `ragpilot upgrade` — applies pending schema migrations, backing up automatically first.
- `ragpilot update [check|status|install]` — checks for, and installs, a newer RAGpilot release (distinct from `upgrade`'s schema migrations). Bare `ragpilot update`/`update check` queries GitHub; `update status` reads the local cache only; `update install` detects how RAGpilot was installed (install script, pip, pipx, or an editable/dev checkout) and upgrades accordingly, then runs schema migrations and a health check against the newly-installed code. Normal commands never make a synchronous GitHub request for this: a lightweight detached check runs at most once every `updates.check_interval_hours` (default 24), and the next invocation notifies you once, to stderr, if a newer version was found. Disable entirely with `ragpilot config set updates.enabled false` or `RAGPILOT_UPDATES__ENABLED=false`.
- `ragpilot uninstall [--keep-data] [--yes]` — removes the installed application (same install-method detection as `update install`: `pip`/`pipx uninstall`, or the install script's own venv/app/launcher files) and, by default, all of its data (databases, config, backups, logs). Prompts for confirmation first unless `--yes`/`-y` is given; `--keep-data` removes only the application. An editable/dev install is never auto-removed — it reports how to remove it manually instead, purging data (unless `--keep-data`) regardless.

### Optional: semantic search & AI-assisted answers
Everything above works fully offline with no LLM. Two opt-in extras layer on top:
- **Semantic search** (`ragpilot config set search.semantic true`): local sentence embeddings (no network once the model is cached) surface similarity-based results as their own clearly lower-confidence tier, never mixed into exact/graph matches. Backed by a persistent local ANN index (`usearch` HNSW by default, auto-falling back to a pure-Python scan only if the `usearch` package itself can't load) for large knowledge bases, updated incrementally as you index and kept warm in memory across repeated queries.
- **`ragpilot ask "QUESTION"`**: runs the same deterministic retrieval as `explore`, then hands the question and that evidence to a configured LLM provider (OpenAI, Anthropic, Ollama, or any OpenAI-compatible endpoint) for a synthesized, evidence-grounded answer. Cloud providers require explicitly opting in (`privacy.external_ai_allowed: true`); a local Ollama endpoint is exempt only when it actually resolves to loopback.

See `CHANGELOG.md` for a detailed history of what shipped, `CONTRIBUTING.md` for development setup, and `SECURITY.md` for the security policy.
