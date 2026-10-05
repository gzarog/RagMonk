# ADR 0025: MCP stdio server (RUST-13)

Status: accepted (decision 2, omitting `ragmonk_ask`, is superseded by ADR 0026)

## Decisions (user)

1. The JSON-RPC layer is hand-rolled (newline-delimited JSON-RPC 2.0
   over stdin/stdout). No MCP SDK or async runtime is added.
2. `ragmonk_ask` is not served. AI features are outside the rewrite so
   far; the server exposes the eight read-only tools.
3. Parity is proven against golden fixtures captured from the reference
   FastMCP server over stdio.

## Command

`ragmonk serve --mcp` behaves like the reference:

- Without `--mcp` it fails with the reference's `UsageError` (exit 2).
- With `mcp.enabled=false` it fails with the reference's `ConfigError`.
- It prints `RagMonk MCP server starting on stdio...` to stderr. Nothing
  but protocol messages is ever written to stdout.
- EOF on stdin ends the server with exit code 0.

## Protocol

- `initialize` returns the reference's capabilities. The requested
  protocol version is echoed when supported (2024-11-05, 2025-03-26,
  2025-06-18, 2025-11-25); otherwise the latest is returned.
  `serverInfo` is `ragmonk v<version>` / `1.30.0` (the reference's MCP SDK
  version, kept for client compatibility).
- `ping`, `tools/list`, `tools/call`, and empty `prompts/list`,
  `resources/list` and `resources/templates/list`.
- Notifications get no reply. An unknown method gets the reference's
  `-32602 "Invalid request parameters"` error. An unparseable line gets
  the reference's `notifications/message` error log.

## Tools

- The catalog (`crates/ragmonk-cli/src/mcp_tools.json`) is the
  reference's `tools/list` output minus `ragmonk_ask`: names,
  descriptions, input schemas and output schemas are byte-for-byte the
  reference's. The generator rewrites it.
- **Arguments** are validated against the input schema with pydantic's
  rules and messages: required fields, strings, and lax integers (numeric
  strings and whole floats are coerced). Extra arguments are ignored.
  Failures are `isError` results with the reference's text. Unknown tools
  get `Unknown tool: <name>`.
- **Work** reuses the CLI's query functions (`symbol_value`,
  `calls_value`, `impact_value`, `explore_value`, `lexical_value`,
  `docs_rows`, `status_cmd::collect`), so results match the CLI's
  `--json` data. Explore's output is remapped like the reference
  (`summary` → `answer_context`, `symbols` → `entities`, `call_flows` →
  `relationships`, plus `warnings`). `max_chars`, `max_files` and
  `max_graph_nodes` override the context budget per call.
- **Output** is projected through the tool's output schema, the way the
  reference's pydantic models filter fields: only schema fields, schema
  defaults for missing ones. Every result carries `schema_version`, `ok`
  and `error`.
- **Errors** become `ok: false` results typed by the reference's
  exception class name (`UsageError`, `ConfigError`, ...), `timeout`
  (after `mcp.request_timeout_seconds`, on a worker thread that is then
  abandoned, as in the reference) or `internal_error` (a panic).
- Each tool call loads the config and opens the sources afresh, like a
  CLI invocation.

## Divergences

- The `content` text is the same JSON as `structuredContent`, but with
  sorted keys rather than the reference's model field order.
- The `initialize` instructions leave out the `ragmonk_ask` sentence.

## Verification

- `compat/tools/gen_mcp_golden.py` indexes the compat code+docs corpus
  with the reference. It then drives `ragmonk serve --mcp` through a
  27-message session covering the protocol, validation errors, and each
  tool's success and error paths. It records normalized responses in
  `compat/golden/mcp.json`.
  - Normalization: corpus, home and source id are masked, opaque ids are
    dropped, and volatile values (timestamps, pids, sizes) are masked.
  - Arrays with tied ranks are sorted.
- `crates/ragmonk-cli/tests/mcp.rs` indexes the same corpus with the Rust
  binary and replays the session, applying the same normalization. Every
  response matches.
