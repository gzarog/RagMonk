# ADR 0012: Cross-domain knowledge linker and manual links (RUST-08)

Status: accepted

## Context

This ports `src/ragmonk/knowledge/linker.py` from Python. The linker connects
code entities to document chunks. Manual links are persisted.

## Decisions

### Linker

- **Crate.** `ragmonk-knowledge` provides the `KnowledgeLinker`, which
  implements `BuildFinalizer`. It is registered after the code finalizers in
  `ragmonk_convert::registry`.
- **Finalizer interface.** `BuildFinalizer::finalize` now receives the ids of
  the files written in the build. This allows incremental (touched-only)
  linking, as in Python.
- **Matchers.** The five Python matchers are ported: exact identifier,
  qualified name, alias, filename and route heuristic. Each one sets the same
  resolver and confidence values as Python.
- **Matching index.** An inverted word index over document units narrows the
  candidates before the boundary check.
- **Boundary rule.** The rule is `(?<![\w.])needle(?![\w.])`, where `\w` means
  Unicode categories L* and N* plus `_`.
- **Deliberate Python quirk.** A mention followed by `.` (for example
  `cancelOrder.` at the end of a sentence) does **not** match. This is kept on
  purpose so that the link set stays identical to Python's. Changing it would
  be a behavior change for a later phase.
- **Deduplication.** Links are deduplicated on (entity, document, chunk,
  resolver), and the first link wins. Rows are written in batches of 5000
  with `INSERT OR IGNORE`.

### Incremental builds

- Links of touched files are removed with their rows. Touched files are then
  matched in both directions against the whole corpus.
- A test checks that incremental relinking produces exactly the same link set
  as a cold build.

### Manual links (behavior change from V1)

- **Storage.** Manual links live in `manual_links`, keyed by:
  - entity qualified name;
  - document relative path;
  - attachment index;
  - chunk ordinal.
- **Migration v4.** Migration v4 (`manual_link_sections`) adds the attachment
  and chunk columns, with a default of -1, and makes the full key UNIQUE. The
  migration is versioned and additive.
- **Behavior.** Every build re-applies manual links as resolver `user`, with
  confidence `exact` and relation `documented_by`. Python V1 dropped manual
  links when a file was reprocessed; V2 keeps them across rebuilds until they
  are removed explicitly.

## Compatibility evidence

- `compat/golden/links.json` holds 23 links, generated from the Python CLI by
  `compat/tools/gen_link_golden.py` on `compat/fixtures/linking`.
- The Rust link set is identical to it.

## Benchmark

The corpus has 2,000 code modules and 100 markdown documents, which gives
14,000 entities, 4,100 units and 12,000 links.

| | Python | Rust |
|---|---|---|
| Linking (all files touched) | 47.09 s | 2.66 s |
| Cold index (end to end) | 78.81 s | 6.60 s |

## Limitations

- Server backends (OpenSearch/Elasticsearch) linking reads arrive with
  RUST-12.
- The CLI `link` commands arrive with the CLI phase. This phase provides the
  library API: `ragmonk_knowledge::manual::{add, remove}`.
