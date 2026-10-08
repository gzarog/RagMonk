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
| **EML** | Subject, headers and body · supported attachments indexed as child documents (see below) |

- A corrupt or unsupported file is isolated and recorded as failed; the run continues.
- Older binary `.doc` / `.ppt` / `.xls` files and `.rtf` are detected but not converted.
- PDF conversions are cached by content hash; unchanged PDFs skip the layout model.
- Chunk sizes follow the embedding model's real tokenizer (`documents.chunking.max_tokens: auto`); tables split at row boundaries.

## Email attachments

An `.eml` file's MIME attachments in any supported format above (PDF, DOCX, XLSX, PPTX, TXT, …) are converted with the same pipeline as standalone files and indexed as **child documents of the email**. The `.eml` stays the only indexed file: no extra file rows, queue jobs or status counts are created for attachments.

- Search results for attachment text keep the real `.eml` path and add `location.attachment` (`name`, `content_type`, `format`, `index`, `parent_title`); the CLI prints `Attachment: <email.eml> -> <name>`.
- Changing or deleting the `.eml` replaces or removes all of its attachment knowledge.
- Unsupported (`.zip`, `.exe`, …), empty, oversized or corrupt attachments are skipped or reported without affecting the email body. Archives are not unpacked and attached emails (`message/rfc822`) are not recursed into.
- Filenames are display metadata only; attachments are written under internal temporary names and removed after conversion.

| Setting | Default | Effect |
|---|---|---|
| `documents.email_attachments` | `true` | `false` restores body-only `.eml` indexing. |
| `documents.email_attachment_max_bytes` | `26214400` (25 MiB) | Largest decoded attachment converted. |
| `documents.email_attachment_max_count` | `50` | Attachments processed per email. |
| `documents.email_attachment_total_max_bytes` | `104857600` (100 MiB) | Total decoded attachment bytes per email. |

Toggling `email_attachments` reprocesses `.eml` files (only those) on the next index; previously indexed emails pick up their attachments without being touched.

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

## Indexing speed

| Setting | Default | Effect |
|---|---|---|
| `documents.pdf_mode` | `accurate` | `fast` reads only the PDF text layer (no layout or table models, no model loading). On a 24-PDF test corpus: 81s to 1.6s. Loses heading/table structure and multi-column reading order; low-text PDFs still follow `documents.ocr`. |

Changing `pdf_mode` reprocesses PDFs on the next index (each mode has its own conversion-cache entries).

Cross-domain linking (code identifiers mentioned in documents) uses a word index plus a pattern cache instead of testing every identifier against every document section: a 2,100-file cold index spent 222s linking before and 0.7s after, with identical links.

## Indexing behaviour

- `ragmonk index` keeps going when one source fails, reports it, and exits non-zero at the end.
- **Last Scan** is the last completed filesystem scan; a later failure shows up as **Last Error**.
- An unreachable source is marked **OFFLINE** and its knowledge is never deleted.

## Status and observability

`ragmonk status` answers: is indexing running, progressing, stalled or failing, and what failed.

| Flag | Effect |
| --- | --- |
| *(none)* | Indexer panel, health summary, per-source table, top problems |
| `--watch [--interval 2]` | Redraws in place with deltas and files/s throughput; Ctrl+C exits |
| `--errors` | Only problematic sources, problems and recent errors |
| `--verbose` / `-v` | Lock owner, progress timestamps, oldest pending job, next retry, per-source last file error, backend counts |
| `--json` | Full structured model (not combinable with `--watch`) |

Terms:

- **Access** (`access_state`): whether the source root is reachable: `online`, `offline`, `disabled`. It says nothing about indexing activity.
- **Index State** (`index_state`): first match wins: `offline` > `stalled` > `indexing` > `retrying` > `errors` > `waiting` > `completed` > `idle`.
- **Last Scan** (`last_scan_at`): last completed filesystem scan, not end-to-end indexing completion.
- **Last Activity**: the live progress heartbeat for the source being indexed, otherwise its last scan.
- **Indexer state**: `running` (index lock held or a live process reports progress), `stalled` (as running, but no progress heartbeat for `indexing.status_stall_threshold_seconds`, default `120`), `crashed` (progress says running but the lock is free and the process is gone; historical, not active), `idle`. A non-empty queue alone never means stalled. Long stages without per-file progress (linking, embeddings) send heartbeats too; a single very large PDF conversion does not, so on huge PDFs raise the threshold.
- **Health**: `failed` if any `error` problem (backend unreachable, stalled or crashed run, fatal run failure, all sources offline), `degraded` if any `warning` (failed files, retries, an offline source, a source-level last error), else `healthy`. Every verdict is explained by `problems`.

Live progress is written by every indexing entry point (CLI, rebuild, daemon, Admin UI) to `<RAGMONK_HOME>/index_progress.json` (atomic write-then-rename, coalesced to at most about one counter write per second).

JSON additions (existing `sources`, `backend`, `totals`, `tokenizer` keys are unchanged): top-level `health`, `indexer`, `queue` (`queued`/`processing`/`retry`/`failed`/`depth`, `oldest_pending_created_at`, `next_retry_at`, `latest_job_error`, ...), `recent_errors` (bounded, 10), `recent_error_count`, `sources_with_errors`, `problems`; per source `access_state`, `index_state`, `queue`, `last_activity_at`, `last_error_detail`. The MCP `ragmonk_status` tool returns the same fields.

## Index locking

- `locks/index.lock` serializes indexing **writers** (CLI, daemon/watch, Admin UI, rebuild, backup); readers/search are never blocked by it.
- Waiting is bounded by `indexing.lock_timeout_seconds` (default `30`, must be > 0 and <= 3600). On timeout the error names the likely owner (PID, operation, source), e.g. `Another RagMonk process holds index.lock (PID 44884, operation=index, source=src_example); timed out after 30s`.
- `ragmonk index` holds the lock for one source pass at a time; the daemon retries a contended source after a short backoff.
- Find the competing process with `ragmonk doctor` (**Index Lock** section) or the PID in the error. Owner metadata is informational only (PIDs can be reused).
- Do **not** delete `index.lock` to recover: the OS lock is released automatically when its owner exits or crashes, and deleting the file can break exclusion.

## Admin UI

`ragmonk ui` → http://127.0.0.1:8765. Manage sources, index with live progress, browse document chunks, test search, explore the code graph, edit config, control the daemon, backups and logs. Binds to `127.0.0.1` only, with CSRF protection and host-header validation. See [ui.md](ui.md).

## MCP (AI agents)

```bash
ragmonk serve --mcp   # register this command as a stdio MCP server in your client
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

- Server support is built into every binary and the container image; there is nothing extra to install.
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
ragmonk doctor
ragmonk update check|status|install|rollback
ragmonk vectors rebuild
ragmonk uninstall [--keep-data]
ragmonk config get|set KEY [VALUE]
```

## Updates

`ragmonk update` follows GitHub's latest **full** release. To also receive
pre-releases, for example to test a release candidate, run:

```bash
ragmonk config set updates.channel prerelease
```

The default is `stable`. Any other value is treated as `stable`.
`updates.enabled`, `updates.check_interval_hours` and `updates.notify`
control the background check and the "newer version" notice.
