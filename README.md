<div align="center">

# 🧘 RagMonk

### Ask your company documents, and your code, anything.
### Answers with exact sources. Entirely on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)
[![Zero Telemetry](https://img.shields.io/badge/telemetry-none-brightgreen)](https://github.com/gzarog/RagMonk)

</div>

---

## 1 · The problem

> *"What does the onboarding policy say about remote equipment?"*
> *"Which spec describes settlement retries, and what code implements them?"*

- Knowledge is scattered across **PDFs, Word, slides, spreadsheets, wikis** and **repositories**
- Typical RAG tools ship that data to the cloud and answer **without sources**

---

## 2 · RagMonk

A **local-first knowledge engine**: point it at folders, get an evidence-backed knowledge base.

- **Deterministic**: full-text, symbols, call graphs, doc↔code links, no LLM needed
- **Evidence-first**: every answer cites file, page, heading or line
- **Private**: no telemetry, no cloud, no external AI unless you opt in

---

## 3 · Documents first

**PDF · Word · PowerPoint · Excel · CSV · HTML · Markdown · OpenDocument · EPUB** (+ OCR for scans)

```
docs/onboarding-policy.pdf  →  page 4 · Remote Work Policy › Equipment Allowance
contracts/msa-2024.docx     →  page 12 · Termination › Notice Period
```

- Headings, paragraphs and tables kept intact, with page provenance
- Incremental: only changed files are reprocessed

---

## 4 · Then the code

**Python · JavaScript · TypeScript · Go · Java · Rust · C#**

```bash
ragmonk callers SettlementService
ragmonk impact  SettlementService    # callers, tests, and the specs that mention it
```

- Symbols, calls, inheritance, HTTP routes
- **Documents ↔ Code**: specs are linked automatically to the code they describe

---

## 5 · One question, one command

```bash
ragmonk explore "what breaks if SettlementService changes?"
```

A deterministic planner combines symbol lookup, graph, full-text, document links and semantic search into **one cited evidence package**.

Also in the **Admin UI** (`ragmonk ui`) and for **AI agents** via MCP (`ragmonk serve --mcp`).

---

## 6 · Install

```bash
curl -fsSL https://raw.githubusercontent.com/gzarog/RagMonk/main/install.sh | sh      # macOS / Linux
```

```powershell
irm https://raw.githubusercontent.com/gzarog/RagMonk/main/install.ps1 | iex           # Windows
```

Python 3.12+ · runs offline after the first model download.

---

## 7 · Quick start

```bash
ragmonk init
ragmonk source add ~/company-docs
ragmonk source add ~/code/payments
ragmonk index
ragmonk explore "what does the onboarding policy say about equipment?"
```

---

## 8 · Scale & summary

- **Local** by default (SQLite) · **OpenSearch / Elasticsearch** for large or shared corpora
- Optional local semantic search and AI answers (Ollama, OpenAI, Anthropic, …)

<div align="center">

**📄 Documents** · **💻 Code** · **🔗 Linked** · **🔒 Local**

</div>

📘 Full details: [docs/reference.md](docs/reference.md) · [CHANGELOG](CHANGELOG.md) · [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md)
