# Contributing to RagMonk

## Development setup

RagMonk targets Python 3.12+.

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
mypy src/ragmonk           # type-check
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

Every other Phase 3 format (DOCX, PPTX, XLSX, HTML, Markdown, TXT, EML),
plus Search Quality Improvement Plan Phase 10's CSV/ODT/ODS/ODP/EPUB, is
converted by Docling's rule-based backends and never touches that
download path, so those tests run in the default suite like everything
else. CI runs `docling_pdf` tests too, but in a separate,
non-blocking (`continue-on-error`) job -- see `.github/workflows/ci.yml`.

Phase 10's one exception is raw images (`.png`/`.jpg`/`.jpeg`/`.tif`/
`.tiff`, gated behind the opt-in `documents.image_ocr` config field):
real text extraction from an image always means a real OCR pass (no
embedded text layer to fall back to the way PDF has), so it reuses this
same marker rather than introducing a new one -- see
`test_docling_pdf.py::test_image_ocr_enabled_extracts_real_text_via_ocr`
and `test_document_indexing.py::
test_image_ocr_enabled_indexes_real_ocr_text_end_to_end`.

#### What `docling_pdf` actually proves for PDF

PDF is normalized straight off Docling's real PDF pipeline output -- the
native `DoclingDocument` it produces, headings/tables/text items and
every item's real `prov` page provenance, exactly as Docling built it.
Because that pipeline is expensive (a real layout/table-structure ML
model), `docling_adapter.convert` caches this document -- keyed by the
file's content hash, in `document_conversion_cache` (a project's
`knowledge.db`) -- as its own JSON serialization (`DoclingDocument`'s
pydantic `model_dump_json()`/`model_validate_json()`), and a cache hit
deserializes straight back into the same document rather than re-running
the pipeline. See `docling_adapter.py`'s module docstring for the full
rationale, including why this replaced an earlier design (Phase 3 through
the search-quality improvement plan's Phase 1A) that cached a *Markdown
export* of the document and reparsed it through a second, separate
Docling backend -- reconstructing page numbers via a page-break marker
string, since that reparsed document carried no `prov` of its own.

`pytest -m docling_pdf` is what actually exercises the real pipeline: it
proves a PDF converts to a normalized unit with correct page provenance
(`test_pdf_converts_to_a_paragraph_with_page_provenance`), that a first
`convert()` call against a project connection populates the cache, and
that a second `convert()` call against that same connection -- including
for a duplicate or moved/renamed copy of the same PDF bytes -- reuses the
cached document without ever touching the PDF-pipeline singleton again
(`test_convert_caches_the_native_document_by_content_hash`/
`test_convert_reuses_cached_document_without_reconverting`/
`test_duplicate_pdf_with_identical_content_reuses_the_cache`/
`test_moved_pdf_with_identical_content_reuses_the_cache`), and that a
stale `cache_version` is treated as a miss rather than reused
(`test_stale_cache_version_causes_reconversion`). The JSON serialization
scheme itself -- that it round-trips headings, paragraphs, tables, and
each item's `prov` losslessly -- is proven separately, without any real
PDF or model, in `tests/unit/test_document_conversion_cache.py`'s
`test_docling_document_json_round_trips_headings_paragraphs_and_tables`,
which runs in the default suite.

### The `daemon_subprocess` marker

`ragmonk daemon start`/`stop` spawn and signal a real, detached OS
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

### The `reranker_model` marker

Search Quality Improvement Plan, Phase 11's optional (`search.reranker.
enabled`, `false` by default) neural reranking pass
(`retrieval/neural_reranker.py`) loads a real `cross-encoder/ms-marco-
MiniLM-L-6-v2` model via `transformers`, which downloads and caches its
weights from Hugging Face on first use -- the same real-network-dependency
shape as `embedding_model`, kept as its own marker rather than reused
since the two load independent models behind independent config flags.
Tests that actually load the model are marked `@pytest.mark.reranker_model`
and excluded from the default run:

```bash
pytest -m reranker_model -q
```

Everything else -- config gating, top-N selection, batching, ordering, and
the graceful-fallback path when the model can't be loaded -- is tested
with a stub scorer and runs in the default suite. CI runs `reranker_model`
tests too, in the same non-blocking style as `embedding_model` -- see
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
RAGMONK_BENCHMARK_SIZE=medium pytest -m benchmark_search -q -s
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

### The `cli_startup_benchmark` marker and `benchmarks/cli_startup/`

The CLI performance improvement plan's startup benchmark
(`benchmarks/cli_startup/`, a top-level package alongside
`benchmarks/search/`) measures subprocess wall-clock startup for the
lightweight commands (`version`, `--help`, `config --help`, `status`,
`search --help`) that should start almost immediately. Its pytest
coverage (`tests/integration/test_cli_startup_benchmarks.py`) is marked
`@pytest.mark.cli_startup_benchmark` and excluded from the default run
the same way `benchmark_search` is, since its millisecond numbers are
only meaningful on real, unshared hardware. Run it explicitly:

```bash
pytest -m cli_startup_benchmark -q -s
python -m benchmarks.cli_startup --strict
```

`--strict` is the form that actually enforces `targets.py`'s warm-start
budgets -- meant for a real developer machine, not CI, which runs
`cli_startup_benchmark` tests in the same non-blocking style as
`benchmark_search` (see `.github/workflows/ci.yml`) and only asserts the
benchmark itself runs (every command completes), never the wall-clock
numbers.

CLI startup has a separate, always-on regression test that *is* a hard
CI gate: `tests/unit/test_cli_startup_imports.py` asserts the lightweight
commands never import Docling/torch/transformers/mcp/openai/anthropic/
usearch, regardless of what a shared runner's timing looks like on any
given run.

### The install scripts (`install.sh` / `install.ps1`)

`install.sh` and `install.ps1` at the repo root are what `README.md`'s
`curl | sh` / `irm | iex` one-liners run. They download a branch/tag
tarball or zipball from GitHub (`RAGMONK_REF`, default `main`), create a
venv, `pip install` the package into it, and link/launch `ragmonk` from a
per-user bin directory (`RAGMONK_BIN_DIR`, `RAGMONK_INSTALL_DIR` to
override). Run them locally exactly as CI does, pointed at a branch:

```bash
RAGMONK_REF=my-branch RAGMONK_INSTALL_DIR=/tmp/ragmonk-install RAGMONK_BIN_DIR=/tmp/ragmonk-bin sh ./install.sh
/tmp/ragmonk-bin/ragmonk version
```

```powershell
$env:RAGMONK_REF = "my-branch"; ./install.ps1
```

Like `docling_pdf`/`embedding_model`, this repeats a real network fetch
plus torch/docling's dependency download, so CI runs it in a separate,
non-blocking (`continue-on-error`) job across all three OSes -- see
`.github/workflows/ci.yml`.

## Test isolation

Every test must isolate RagMonk's runtime directory via the `RAGMONK_HOME`
environment variable (see the `ragmonk_home` fixture in `tests/conftest.py`)
and a `tmp_path`-based current working directory where project config or
source files are involved. Tests must never read or write the real
`~/.ragmonk` directory.

## Project layout

- `src/ragmonk/cli/` — Typer CLI commands
- `src/ragmonk/core/` — config, paths, models, errors, app lifecycle
- `src/ragmonk/sources/` — source registry, scanning, ignore rules, hashing
- `src/ragmonk/storage/` — SQLite connection handling, schema, migrations, repositories
- `src/ragmonk/indexing/` — scan/classify/enqueue/process orchestration
- `src/ragmonk/code/` — Tree-sitter parsing, extraction, resolution, framework heuristics
- `src/ragmonk/documents/` — Docling adapter, normalization, chunking, metadata
- `src/ragmonk/retrieval/` — lexical/semantic search, graph traversal, context budgeting
- `src/ragmonk/ai/` — LLM provider abstraction for `ragmonk ask` (Phase 9)
- `src/ragmonk/telemetry/` — structured logging
- `src/ragmonk/security/` — path containment and secret-file exclusion

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
