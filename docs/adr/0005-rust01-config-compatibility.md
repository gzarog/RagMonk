# ADR 0005 — RUST-01 core/config compatibility decisions

Status: accepted (RUST-01)

## Evidence
`rust/compat/tools/gen_core_golden.py` runs the Python reference over
259 configuration cases (YAML files, env layers, project/user layering),
source/project/server-document ID inputs, redaction inputs, lock-timeout
messages, secret filename patterns and home layout, and writes
`rust/compat/golden/core_*.json`. The Rust crates replay every case:

* `ragmonk-config` reproduces PyYAML `safe_load` (YAML 1.1 implicit types,
  merge keys, anchors, duplicate keys), pydantic v2 lax coercion and error
  text (including error ordering and union locations), and
  `yaml.safe_dump(sort_keys=False)` byte-for-byte (quoting, `\xNN`
  escapes, folding at width 80, float representation).
* `ragmonk-core` source IDs, project IDs and the OpenSearch/Elasticsearch
  deterministic `_id`s match both Python modules.
* `ragmonk-telemetry` URL/credential redaction matches, including
  Python 3.12 `urlsplit`/`urlunsplit` behavior.

The compat-harness gate (`ragmonk-compat gate --phase RUST-01`) also
shows `config show`, `config get <scalar>` and `config get <unknown>`
(stdout, stderr, exit code) identical to Python.

## Documented divergences
1. **YAML error text.** Syntax/constructor errors keep the
   `failed to read config file <path>: ` prefix, the first line of the
   reason for constructor errors and exit code 3; PyYAML's position
   snippet is not reproduced.
2. **Integer range.** Python ints are unbounded; config integers are
   `i64`. Values beyond that range are rejected with a clear message
   instead of being accepted. No realistic setting needs them.
3. **Non-ASCII digits in env vars.** Python `int("٣")` is 3; the Rust
   env layer only accepts ASCII digits and treats such values as strings.
4. **`version --json`.** `data.python` (interpreter version) is replaced
   by `data.runtime: "rust"`; the compat manifest drops both keys.
5. **Log records** additionally pass string field values through URL /
   credential redaction (hardening; identical output when no credentials
   are present).
6. **`config set` validation failures** keep exit code 1 but print
   `invalid configuration: <loc>: <msg>` instead of pydantic's multi-line
   `ValidationError` dump (which echoes input values). Full CLI rendering
   parity is RUST-12 scope.
7. **`config get` of a mapping/list** prints a Python `repr` on one line;
   Python prints it through `rich`'s pretty printer (RUST-12 scope; the
   manifest step is owned by RUST-12).

## Scope notes
* Lock acquisition/owner metadata (`core/lifecycle.py` `RunLock`) and
  `AppContext` belong to RUST-04 (indexing coordination); RUST-01 ports
  only the `RunLockTimeoutError` message contract.
* The toolchain is pinned to Rust 1.90.0 (MSRV 1.88) because current
  `yaml-rust2` dependencies (`encoding_rs`) require rustc 1.88.
