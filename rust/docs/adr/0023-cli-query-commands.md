# ADR 0023: CLI query commands (RUST-12, slice 2)

Status: accepted

## Commands

- **`symbol`, `callers`, `callees`, `references`.**
  - Each runs over every indexed source.
  - `--json` returns the reference's `{query, matches, edges}`.
  - Text output is the reference's tables.
  - `--max-depth` is limited to 1–10 and `--limit` to 1–1000.
- **`impact`.** The RUST-10 payload. The text view lists defined at,
  callers, callees, tests, documentation, confidence and blast radius.
- **`explore`.**
  - Runs the RUST-10 planner and payload.
  - When the plan includes semantic search and the model is installed,
    semantic search runs.
  - The context budget comes from the `context.*` settings.
- **`search`.**
  - Supports `--json`, `--snippets`, `--table`, `--explain`, `--hybrid`
    and `--limit` (1–200). The default output mode is
    `search.output.fallback[0]`.
  - `search.lazy_semantic` skips semantic search when lexical confidence
    is already high.
  - `--hybrid` merges lexical and semantic results and reranks them, with
    the optional cross-encoder pass.
  - `--explain` reports:
    - the query kind;
    - the lexical confidence;
    - whether semantic search was skipped;
    - per-stage timings;
    - the tokenizer identity.
- **`link add`, `remove`, `list`.**
  - `add` resolves the entity (by id, name or qualified name) and the
    document (by id, path, path suffix or filename) in exactly one source,
    with the reference's ambiguity errors. `--section` takes a chunk id.
  - Manual links are the RUST-08 ones: they are stored by qualified name
    and path, and survive every rebuild.
  - `list` shows a user link's manual id, which is what `remove` takes.
  - Automatic links are listed but cannot be removed. The next pass would
    recreate them, which the reference's delete also does not survive.
    This is the one intended difference from the reference.

## Supporting work

- **Absolute paths.**
  - Retrieval reports paths relative to the source root, while the
    reference prints absolute ones.
  - The CLI joins search result paths, path-hit titles, semantic hit
    paths, and every `path`, `paths` and `source_location` field in
    `explore` and `impact` payloads with the source's path.
  - It does this in the CLI because the retrieval crate's relative API and
    its golden tests are unchanged.
- **Query classification (`retrieval::classify`).** A port of
  `query_classifier.py`: `classify_query` (symbol, path, keyword or
  conceptual) and `estimate_confidence` (high, medium or low).
- **Context expansion (`retrieval::context`).**
  - A port of `expand_chunk_context`, deferred from ADR 0016.
  - A matched document chunk gets its enclosing heading and its nearest
    siblings under that heading, within `search.context.max_tokens`.
  - Token costs use the bundled tokenizer. The matched chunk is never
    dropped. Siblings are taken nearest-first, so the window stays
    contiguous.
  - A table's text is its row-aware rendering, which is what the reference
    stores.
- **Chunk parents (storage).**
  - Knowledge migration 6 adds `chunks.parent_ordinal`: the chunker's
    `parent_index`, which was computed but not stored. It is the
    reference's `document_sections.parent_id`.
  - `ProjectStore::chunk`, `chunk_at` and `chunk_siblings` read it.
  - The stored chunker identity gains a Rust-only `+chunk-parents.1`
    suffix, so builds made without parents are rebuilt once.
    `CHUNKER_VERSION` still matches the reference stamp.
- **Unordered arrays in the compat harness.** These are now sorted by
  key-sorted JSON text. Previously the sort depended on each
  implementation's key order, which put equal edges in different orders.

## Compat gate

- **Exclusion removed.** The RUST-10 exclusion from slice 1 is gone. CI
  now gates every step at RUST-12.
- **New steps.** The manifest adds:
  - `references speak`;
  - `impact speak`;
  - `explore Dog`;
  - `search "Main Heading"`, which exercises context expansion on a
    document;
  - `link list`.
- **Unordered steps.** Two of these steps compare their arrays unordered:
  - `link list` (`links`): the reference orders links by its random ids.
  - The search step (`results`): its tied title and heading hits fall
    back to the reference's random ids too. Ranking itself stays covered
    by the RUST-10 search goldens.
- **Result.** All 23 gated steps match the Python reference with no
  canonical differences. The new steps are stable across three Python
  runs.

## Tests

- `ragmonk-cli/tests/queries.rs` covers, end to end:
  - text and JSON modes;
  - absolute paths;
  - range checks;
  - search output modes and `--explain`;
  - link add, list and remove, including duplicates, errors and survival
    across a rebuild.
- `ragmonk-retrieval/tests/context.rs` covers heading and sibling
  structure, table rendering and the token budget, on a real indexed
  Markdown document.
