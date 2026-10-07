# ADR 0001 — Compatibility policy

Status: accepted (RUST-00)

## Context
RagMonk is being rewritten from Python to Rust (`RagMonk_Full_Rust_Rewrite_V1`).
User homes, SQLite databases and OpenSearch/Elasticsearch indexes are user
data, and CLI/MCP JSON is consumed by agents.

## Decision
* The Python implementation at `1e92ed42e258898572e8dc57b1ce2608435686d9`
  (and its release-branch lineage) is the reference until RUST-16.
* Behavior is migrated only behind differential evidence: the same fixture
  runs through Python and Rust via `ragmonk-compat`, both outputs are
  canonicalized, and the diff must be empty (floats within tolerance).
* Canonicalization masks only *documented* nondeterminism: timestamps,
  durations, PIDs/hostnames, environment paths and random opaque IDs.
* **Finding recorded at RUST-00:** in local mode, file/entity/document/chunk/
  job IDs are random `uuid4().hex` (`code/processor.py`,
  `indexing/coordinator.py`, `documents/pipeline.py`). Their *value* is not a
  contract, their shape (32 lowercase hex) is. Source IDs (`src_` + 10 hex)
  and project directories are path-derived and deterministic; they are
  compared exactly in `--strict-ids` mode (same work path for both
  implementations) and masked in portable baselines. Server-mode
  deterministic document IDs are covered in RUST-03.
* Each manifest step names the `rust_phase` that must make Rust pass it. A
  phase cannot merge while a step it owns differs.
* Intentional behavior changes need a versioned migration, an ADR, and an
  updated baseline in the same PR.

## Consequences
Baselines are committed under `rust/compat/baseline/` and regenerated only
deliberately. Python dependency ranges are not locked, so a fresh Python
resolution may drift from the baseline; CI reports that drift as
informational, while two back-to-back captures must stay identical.

## Addendum — unordered collections
CI showed the Python reference returns tied `callers`/`callees` edges in a
nondeterministic order (JS `this.speak` vs Python `self.speak`). Edge order
among ties is therefore not a contract. Steps declare such arrays in
`unordered_arrays` and the harness sorts them by canonical form; ranked
result lists (e.g. search) stay order-sensitive.
