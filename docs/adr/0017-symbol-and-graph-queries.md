# ADR 0017: Symbol search and graph queries (RUST-10, slice 2)

Status: accepted

## Scope

The `ragmonk_retrieval::graph` module ports the reference's `code/graph.py`
and `retrieval/graph.py`. Storage support lives in
`ragmonk_storage::search`: `symbol_entities`, `entity_edges`,
`unresolved_edges` and `file_rel_path`, all scoped to one build.

| Reference | V2 |
|---|---|
| `find_symbol_matches` | `find_symbol_matches`: exact name or qualified name, across corpora |
| `traverse` | `traverse`: BFS with `max_depth` and `limit`, per-frontier edge sort `(type, target, id)`, visited set |
| `unresolved_symbol_edges` | `unresolved_symbol_edges` |
| `traverse_symbol` (`callers` / `callees`) | `traverse_symbol` |
| `references` | `references`: CALLS / IMPORTS / REFERENCES, both directions |
| `resolved_incoming` / `resolved_outgoing` | same names; the neighbour entity and its source-relative path |
| `is_test_file` / `find_tests_referencing` | same names |

These reference behaviours are kept:

- **Unresolved edges on incoming walks.** Incoming walks also merge the
  unresolved (name-only) edges recorded under the query and under each
  match's bare name, once per (corpus, name).
- **No match at all.** When nothing matches, incoming falls back to
  unresolved edges in every corpus.
- **Limits.** Depth 1 and limit 100 are the defaults, and a limit stops the
  BFS mid-frontier.
- **Test files.** `is_test_file` reproduces the reference's seven naming
  patterns (a test checks 18 paths against the reference's own function).

## Deliberate difference

The reference orders symbol matches by `(qualified name, source, id)`. Its
ids are random UUIDs, so same-named overloads come out in arbitrary order.
V2 orders them by `(qualified name, source, path, line, id)`.

## Evidence

`compat/golden/graph.json` comes from the reference
(`compat/tools/gen_graph_golden.py`). It covers three corpora:

- the new `fixtures/graph` corpus: call chains, a cycle, unresolved
  callees, and Python, Go and TypeScript test files;
- `fixtures/code`;
- `fixtures/linking`.

Each corpus is queried with every entity name, every qualified name,
unresolved-only names and a missing name: 298 queries. Each query runs at
depths 1 and 3 and records:

- symbol matches;
- callers and callees;
- references;
- resolved incoming and outgoing edges;
- tests.

**Result: all 596 query/depth checks match the reference.** Everything is
compared in id-free form: entity = path, qualified name, kind, line; edges
include the resolver and confidence. Within one depth, edges are ordered by
ids that differ between implementations, so each depth is compared as a
multiset. The depth sequence must match.

**Making the stored graphs comparable.** The reference resolves a call
against the entities stored when the caller's file is processed, so its
stored graph depends on scan order. Re-indexing does not converge either,
because re-parsing a file briefly removes its entities. V2 resolves against
the whole build (ADR 0009). The generator therefore rebuilds the reference's
relationships with its own `_build_relationships` against the whole corpus
before querying, the method the RUST-05 golden uses. The golden then
exercises the query logic on equal data.

## Not in this slice

- **Slice 3:** explore/impact aggregation and attachment-location rendering.
- **RUST-12 / RUST-13:** the CLI and MCP surfaces of these queries.
- **Server mode:** server-mode graph neighbours.
