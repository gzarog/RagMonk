# ADR 0018: explore, impact and attachment provenance (RUST-10, slice 3)

Status: accepted

## Scope

`ragmonk_retrieval::explore` ports these reference modules:

| Reference | V2 |
|---|---|
| `retrieval/planner.py` | `plan`: the same ordered regex rules (impact > callers > callees > document hint > identifier > general). Semantic search is only ever *added* to document and general plans. |
| `retrieval/context_builder.build_context` | `build_context`: dedupe per location (higher confidence wins, ties keep the first), sort by confidence then path/line/page/entity with `None` last, cap by `max_files` / `max_chars` (snippet characters) / `max_graph_nodes`, and report every cap in the truncation reasons |
| `knowledge/evidence.py` | `Evidence`, `EvidenceLocation`, `EvidenceItem`, `GraphPath` |
| `knowledge/document_links.py` | `linked_document_evidence`: one item per distinct (document, chunk) per corpus |
| `cli/impact._run` | `impact`: the same JSON payload, including the blast-radius buckets (≤ 2 LOW, ≤ 7 MEDIUM, else HIGH; score = distinct callers + distinct documented sections) |
| `cli/explore._run` | `explore`: the same JSON payload. Semantic results come from the caller, which holds the embedder. |

**Attachment provenance.** Semantic hits now carry the full attachment
provenance: name, content type, index, format and parent title. Before,
they carried only the attachment index. Lexical hits already did
(slice 1).

## Deliberate difference

The reference orders an entity's links by creation time, then random id.
Links that the linker writes in the same instant therefore pick an
arbitrary winner per (document, section). Example: an exact-identifier
mention against a qualified-identifier `documented_by` link to the same
heading.

V2 picks the strongest link: highest confidence, then the linker's own
resolver order (exact, qualified, alias, filename, route), then link type.
The golden generator applies the same order to the reference.

## Not ported here

- **Chunk context expansion** (`expand_chunk_context`: parent heading and
  sibling chunks around a search hit). It is display decoration for the
  `search` command, and it needs the chunk's parent id, which the V2 chunk
  table does not store yet. It moves to RUST-12 with the `search` output
  modes.
- **Server mode.** Server-mode link reads are deferred.

## Evidence

- **`compat/golden/explore.json`** (`compat/tools/gen_explore_golden.py`)
  covers the `linking` and `graph` corpora:
  - `impact` for every entity name and qualified name plus a missing name,
    at depths 1 and 3: 196 payloads;
  - `explore` for 20 planner-shaped queries (callers, callees, impact,
    document, identifier and general).

  Ids are stripped. Link-, id- and insertion-ordered lists are compared as
  multisets: documentation, documents, evidence, call flows and
  dependencies. **All 216 payloads match.** As in slice 2, the reference's
  relationships are rebuilt against the whole corpus first.
- **`compat/golden/attachments.json`**
  (`compat/tools/gen_attachment_golden.py`) uses a new
  `fixtures/attachments` corpus: an email with text, Markdown, Office and
  archive attachments. Every lexical hit's kind, tier and attachment
  provenance matches the reference in order. The top-5 semantic hits'
  provenance matches as a set.
- **Unit tests** cover the planner examples, context deduplication,
  prioritization and truncation, and the blast-radius buckets.
