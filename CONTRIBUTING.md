# Contributing to RAGpilot

## Development setup

RAGpilot targets Python 3.12+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

This installs the package in editable mode plus the dev toolchain
(`pytest`, `ruff`, `mypy`).

## Running the checks

```bash
pytest -q                 # tests
ruff check .               # lint
mypy src/ragpilot           # type-check
```

All three must pass before opening a pull request; CI runs the same checks
across Linux, macOS, and Windows.

### The `docling_pdf` marker

Docling's PDF pipeline (layout detection, table structure) downloads model
weights from Hugging Face on first use. That is a real network dependency,
so tests that actually invoke it are marked `@pytest.mark.docling_pdf` and
excluded from the default run (`pyproject.toml`'s `addopts` runs
`-m 'not docling_pdf'`). Run them explicitly:

```bash
pytest -m docling_pdf -q
```

Every other Phase 3 format (DOCX, PPTX, XLSX, HTML, Markdown, TXT, EML) is
converted by Docling's rule-based backends and never touches that
download path, so those tests run in the default suite like everything
else. CI runs `docling_pdf` tests too, but in a separate,
non-blocking (`continue-on-error`) job -- see `.github/workflows/ci.yml`.

#### What `docling_pdf` actually proves for PDF

PDF is not normalized straight off Docling's PDF pipeline output. After
that real pipeline produces its `DoclingDocument`, `docling_adapter
.convert` exports it to Markdown, caches the Markdown text by the file's
content hash (`document_conversion_cache`, a project's `knowledge.db`),
and reparses that Markdown through Docling's separate, rule-based
Markdown backend into the document that is actually normalized/chunked/
indexed -- see `docling_adapter.py`'s module docstring for the full
rationale. `pytest -m docling_pdf` is what actually exercises this: it
proves the real pipeline's Markdown export round-trips correctly through
a reparse (`test_pdf_converts_to_a_paragraph_with_page_provenance`), that
a first `convert()` call against a project connection populates the
cache, and that a second `convert()` call against that same connection
reuses the cached Markdown without ever touching the PDF-pipeline
singleton again (`test_convert_caches_markdown_by_content_hash`/
`test_convert_reuses_cached_markdown_without_reconverting`). The marker-
based page-number reconstruction this round-trip requires (reparsed
items carry no `prov`) is proven separately, without any real PDF or
model, in `tests/unit/test_document_normalizer.py`'s
`test_page_break_marker_reconstructs_page_numbers_without_prov` -- that
one runs in the default suite.

### The `daemon_subprocess` marker

`ragpilot daemon start`/`stop` spawn and signal a real, detached OS
process (Phase 7). That is slower and more platform-fragile than
anything else in the suite (process startup time, signal delivery, PID
reuse), so tests that actually spawn a process are marked
`@pytest.mark.daemon_subprocess` and excluded from the default run the
same way `docling_pdf` is (`addopts` runs
`-m 'not docling_pdf and not daemon_subprocess'`). Run them explicitly:

```bash
pytest -m daemon_subprocess -q
```

The daemon *loop* itself -- watcher wiring, debounce, periodic
reconciliation, offline/online transitions, graceful shutdown, PID-file
and health-snapshot bookkeeping -- is fully covered by direct unit/
integration tests (`test_daemon.py`, `test_service_pid.py`,
`test_service_health.py`, `test_watcher_local.py`,
`test_watcher_network.py`) that never spawn a process, so this marker
only guards the process-spawning/signaling plumbing around it. CI runs
`daemon_subprocess` tests too, in the same style as `docling_pdf`: a
separate, non-blocking (`continue-on-error`) job -- see
`.github/workflows/ci.yml`.

### The `embedding_model` marker

Phase 9's local semantic search (`retrieval/embedder.py`) loads a real
`sentence-transformers/all-MiniLM-L6-v2` model via `transformers`, which
downloads and caches its weights from Hugging Face on first use -- the
same real-network-dependency shape as `docling_pdf`'s model download, so
it gets the same treatment: tests that actually load the model are
marked `@pytest.mark.embedding_model` and excluded from the default run
(`pyproject.toml`'s `addopts`). Run them explicitly:

```bash
pytest -m embedding_model -q
```

Everything else in Phase 9 -- vector storage/cosine similarity
(`retrieval/vectorstore.py`), the `embeddings` table repository, semantic
search's degrade-gracefully paths, and the AI provider abstraction -- is
tested with precomputed/fake vectors and mocked HTTP, and runs in the
default suite. CI runs `embedding_model` tests too, in the same style as
`docling_pdf`: a separate, non-blocking (`continue-on-error`) job -- see
`.github/workflows/ci.yml`.

### The `benchmark_search` marker and `benchmarks/search/`

The search performance redesign's benchmark suite (`benchmarks/search/`,
a top-level package alongside `src/` and `tests/` -- see its own
docstring) generates a synthetic corpus directly through the storage
repositories (no Tree-sitter/Docling parsing, no real embedding model)
and measures real query latency against it. Its pytest coverage
(`tests/integration/test_search_benchmarks.py`) is marked
`@pytest.mark.benchmark_search` and excluded from the default run the
same way `docling_pdf`/`daemon_subprocess`/`embedding_model` are, since
its millisecond numbers are only meaningful on real, unshared hardware
(a busy CI runner missing a target is not a regression signal -- see
`benchmarks/search/targets.py`). Run it explicitly:

```bash
pytest -m benchmark_search -q -s
```

That runs the fast `small` (5k-embedding) corpus size. Larger sizes
(`medium`/`large`/`very_large`, per the blueprint's own suggested
scale) are opt-in, both for the pytest coverage and the standalone
script:

```bash
RAGPILOT_BENCHMARK_SIZE=medium pytest -m benchmark_search -q -s
python -m benchmarks.search --size large --strict
```

`--strict` is the form that actually enforces the blueprint's
performance targets (section 36) -- meant for a real developer machine,
not CI, which runs `benchmark_search` tests in the same non-blocking
style as `docling_pdf`/`embedding_model` (see `.github/workflows/
ci.yml`) and only asserts the pipeline itself works (every category
finds its known fixture), never the wall-clock numbers.

Search *quality* (not speed) has a separate, always-on regression suite in
`benchmarks/search_quality/` (Phase 0 of the search-quality improvement
plan) -- see that package's own docstring:

- `benchmarks/search_quality/fixture_project.py`: the one small,
  hand-written project (three code files, three Markdown documents with
  headings and tables) every golden query is evaluated against, shared by
  the test below and the report generator so it can't drift out of sync.
- `benchmarks/search/golden_queries.yaml`: 72 queries across ten
  categories (exact title/heading/symbol/path lookup, keyword search,
  natural-language "semantic_document" queries, table questions,
  cross-document queries, code-to-document queries, and partial/typo'd
  terms), each with an `expected` relevant set and a `category` tag.
- `benchmarks/search_quality/evaluator.py`: runs the golden set through
  the real `retrieval/lexical.search` and computes Recall@1/3/5/10, MRR,
  and NDCG@10 (`benchmarks/search/quality.py`), overall and per category.
- `tests/integration/test_search_quality.py`: indexes the fixture project
  through the real CLI pipeline and asserts both the overall metrics and
  each category's Recall@5 stay at or above this fixture's own known-
  achievable floor. Always-on and blocking (no marker) -- fast, offline,
  fully deterministic -- so a lexical-ranking regression is caught here,
  not just noticed by eyeballing search output.
- `benchmarks/search_quality/report.py`
  (`python -m benchmarks.search_quality`): the full Phase 0 baseline
  report -- golden-query quality plus lexical/semantic/hybrid latency,
  cold-vs-warm semantic search latency, indexing time by file type,
  re-indexing time, chunk/vector counts, and on-disk DB/index size. Its
  real output is committed as `benchmarks/search_quality/
  baseline_report.json`/`.md`, for later phases to diff their own changes
  against. `tests/integration/test_search_quality_report.py` (also marked
  `benchmark_search`) exercises the generator itself without hard-gating
  on its hardware-dependent latency numbers.

### The install scripts (`install.sh` / `install.ps1`)

`install.sh` and `install.ps1` at the repo root are what `README.md`'s
`curl | sh` / `irm | iex` one-liners run. They download a branch/tag
tarball or zipball from GitHub (`RAGPILOT_REF`, default `main`), create a
venv, `pip install` the package into it, and link/launch `ragpilot` from a
per-user bin directory (`RAGPILOT_BIN_DIR`, `RAGPILOT_INSTALL_DIR` to
override). Run them locally exactly as CI does, pointed at a branch:

```bash
RAGPILOT_REF=my-branch RAGPILOT_INSTALL_DIR=/tmp/ragpilot-install RAGPILOT_BIN_DIR=/tmp/ragpilot-bin sh ./install.sh
/tmp/ragpilot-bin/ragpilot version
```

```powershell
$env:RAGPILOT_REF = "my-branch"; ./install.ps1
```

Like `docling_pdf`/`embedding_model`, this repeats a real network fetch
plus torch/docling's dependency download, so CI runs it in a separate,
non-blocking (`continue-on-error`) job across all three OSes -- see
`.github/workflows/ci.yml`.

## Test isolation

Every test must isolate RAGpilot's runtime directory via the `RAGPILOT_HOME`
environment variable (see the `ragpilot_home` fixture in `tests/conftest.py`)
and a `tmp_path`-based current working directory where project config or
source files are involved. Tests must never read or write the real
`~/.ragpilot` directory.

## Project layout

- `src/ragpilot/cli/` — Typer CLI commands
- `src/ragpilot/core/` — config, paths, models, errors, app lifecycle
- `src/ragpilot/sources/` — source registry, scanning, ignore rules, hashing
- `src/ragpilot/storage/` — SQLite connection handling, schema, migrations, repositories
- `src/ragpilot/indexing/` — scan/classify/enqueue/process orchestration
- `src/ragpilot/code/` — Tree-sitter parsing, extraction, resolution, framework heuristics
- `src/ragpilot/documents/` — Docling adapter, normalization, chunking, metadata
- `src/ragpilot/retrieval/` — lexical/semantic search, graph traversal, context budgeting
- `src/ragpilot/ai/` — LLM provider abstraction for `ragpilot ask` (Phase 9)
- `src/ragpilot/telemetry/` — structured logging
- `src/ragpilot/security/` — path containment and secret-file exclusion

This repository is built out in sequential phases (see `README.md`); please
keep changes scoped to the phase you are working on rather than adding
speculative structure for later phases.

## Document fixtures

`tests/fixtures/documents/` holds committed, hand-verified fixtures for
Phase 3's golden tests (`simple.txt`, `simple.md`, `simple.html`,
`sample.eml`, `corrupt.docx`, `sample.pdf` by hand; `document.docx`,
`presentation.pptx`, `spreadsheet.xlsx` generated). Regenerate the latter
with:

```bash
python tests/fixtures/documents/generate_fixtures.py
```

## Commit style

Keep commits focused and describe the "why" as well as the "what". Update
`CHANGELOG.md` under "Unreleased" for user-visible changes.
