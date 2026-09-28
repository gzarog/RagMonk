# RagMonk reference

Detail behind the [README](../README.md) pitch deck.

## Document formats

| Format | What you get |
|---|---|
| **PDF** | Layout model: headings, tables, page numbers · OCR for scanned pages (`documents.ocr: auto`) |
| **DOCX / ODT** | Heading structure with page provenance |
| **PPTX / ODP** | Slide titles and body text, slide-number provenance |
| **XLSX / ODS / CSV** | Tables rendered and chunked row-by-row |
| **HTML / Markdown** | Wikis, docs-as-code, ADRs, runbooks |
| **TXT / EPUB** | Plain text and e-books |
| **PNG / JPG / TIFF** | Opt-in OCR (`documents.image_ocr: true`) |

- A corrupt or unsupported file is isolated and recorded as failed; the run continues.
- Legacy `.doc` / `.ppt` / `.xls` / `.rtf` are detected but not converted.
- PDF conversions are cached by content hash; unchanged PDFs skip the layout model.
- Chunk sizes follow the embedding model's real tokenizer (`documents.chunking.max_tokens: auto`); tables split at row boundaries.

## Search

```bash
ragmonk search "remote work equipment allowance"   # --table · --json · --limit N · --explain · --hybrid
ragmonk docs [--source <id>]                       # list indexed documents
```

## Code

Full extraction for Python, JavaScript, TypeScript/TSX, Go, Java, Rust and C#; HTTP routes from Python decorators and C# route attributes. Other languages are indexed for full-text search only.

```bash
ragmonk symbol | callers | callees | references | impact  <Symbol>
ragmonk link list
ragmonk link add <entity> <document> [--section ID]
ragmonk link remove <ID>
```

## Indexing behaviour

- `ragmonk index` keeps going when one source fails, reports it, and exits non-zero at the end.
- **Last Scan** is the last completed filesystem scan; a later failure shows up as **Last Error**.
- An unreachable source is marked **OFFLINE** and its knowledge is never deleted.

## Admin UI

`ragmonk ui` → http://127.0.0.1:8765. Manage sources, index with live progress, browse document chunks, test search, explore the code graph, edit config, control the daemon, backups and logs. Binds to `127.0.0.1` only, with CSRF protection and host-header validation. See [ui.md](ui.md).

## MCP (AI agents)

```bash
ragmonk install-agent --client claude-code   # or codex · cursor · vscode · all
ragmonk serve --mcp
```

Tools: `ragmonk_explore`, `ragmonk_search`, `ragmonk_documents`, `ragmonk_symbol`, `ragmonk_callers`, `ragmonk_callees`, `ragmonk_impact`, `ragmonk_status`, `ragmonk_ask`.

## Optional AI

```bash
ragmonk config set search.semantic true                  # local semantic search
ragmonk config set ai.provider ollama
ragmonk config set ai.model llama3.2
ragmonk ask "how are settlement retries handled?"
```

Providers: Ollama, OpenAI-compatible, OpenAI and Anthropic (require `privacy.external_ai_allowed: true`), Codex and GitHub Copilot (beta, `ragmonk ai login <provider>`; see [providers/](providers/)).

## Server storage (OpenSearch / Elasticsearch)

```bash
ragmonk init --storage-mode server --storage-engine opensearch \
    --storage-url https://search.internal:9200
```

- The install scripts always include `opensearch-py` and `elasticsearch`; with plain pip use `pip install "ragmonk[server]"`.
- `init` validates the real cluster first (reachable, right engine, OpenSearch 2+ / Elasticsearch 8+, permissions).
- Credentials only via env vars: `RAGMONK_OPENSEARCH_{USERNAME,PASSWORD,API_KEY}` / `RAGMONK_ELASTICSEARCH_{USERNAME,PASSWORD,API_KEY}`. Never stored in `config.yaml`.
- First index and every rebuild publish a new generation atomically; a failed rebuild keeps serving the previous one.
- Local SQLite stays the control plane only (scan state, queue).
- Dev clusters: `docker compose -f docker/docker-compose.opensearch.yml up -d` (or `docker-compose.elasticsearch.yml`).

## Operations

```bash
ragmonk backup [PATH]
ragmonk restore ARCHIVE
ragmonk rebuild [--source ID]
ragmonk upgrade
ragmonk update check|install
ragmonk vectors rebuild
ragmonk uninstall [--keep-data]
ragmonk config get|set KEY [VALUE]
```
