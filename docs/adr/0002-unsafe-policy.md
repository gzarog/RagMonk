# ADR 0002 — `unsafe` policy

Status: accepted

* Workspace lint `unsafe_code = "forbid"` applies to every project crate.
* If a change truly needs `unsafe` (e.g. an FFI shim not covered by a
  maintained crate), it must: relax the lint for that one module only, document
  the invariants in a `// SAFETY:` comment at each block, add focused tests,
  and record the exception in a new ADR.
* `unsafe` inside third-party dependencies (rusqlite, ort, tree-sitter) is
  accepted subject to the dependency policy (ADR 0003).
