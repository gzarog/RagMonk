# ADR 0025: MCP stdio server

Status: accepted

## Decisions

1. The JSON-RPC layer is hand-rolled (newline-delimited JSON-RPC 2.0
   over stdin/stdout). No MCP SDK or async runtime is added.
2. The server exposes the read-only `ragmonk_*` tools: `ragmonk_explore`,
   `ragmonk_search`, `ragmonk_symbol`, `ragmonk_callers`,
   `ragmonk_callees`, `ragmonk_impact`, `ragmonk_documents`,
   `ragmonk_status` and `ragmonk_ask` (ADR 0026).
3. Behavior is pinned by a recorded stdio session
   (`fixtures/expected/mcp.json`).

## Command

`ragmonk serve --mcp`:

- Without `--mcp` it fails with a usage error (exit 2).
- With `mcp.enabled=false` it fails with a configuration error.
- It prints `RagMonk MCP server starting on stdio...` to stderr. Nothing
  but protocol messages is ever written to stdout.
- EOF on stdin ends the server with exit code 0.

## Protocol

- `initialize` returns the tools, prompts and resources capabilities and
  the server instructions. The requested protocol version is echoed when
  supported (2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25); otherwise
  the latest is returned. `serverInfo` is `ragmonk v<version>` /
  `<version>`.
- `ping`, `tools/list`, `tools/call`, and empty `prompts/list`,
  `resources/list` and `resources/templates/list`.
- Notifications get no reply. An unknown method gets
  `-32602 "Invalid request parameters"`. An unparseable line gets a
  `notifications/message` error log.

## Tools

- The catalog (`crates/ragmonk-mcp/src/mcp_tools.json`) holds names,
  descriptions, input schemas and output schemas.
- **Arguments** are validated against the input schema: required fields,
  strings, and lax integers (numeric strings and whole floats are
  coerced). Extra arguments are ignored. Failures are `isError` results.
  Unknown tools get `Unknown tool: <name>`.
- **Work** reuses the application services the CLI uses, so results match
  the CLI's `--json` data. Explore's output is remapped for agents
  (`summary` → `answer_context`, `symbols` → `entities`, `call_flows` →
  `relationships`, plus `warnings`). `max_chars`, `max_files` and
  `max_graph_nodes` override the context budget per call.
- **Output** is projected through the tool's output schema: only schema
  fields, schema defaults for missing ones. Every result carries
  `schema_version`, `ok` and `error`. The `content` text is the same JSON
  as `structuredContent`.
- **Errors** become `ok: false` results typed by the error kind
  (`UsageError`, `ConfigError`, ...), `timeout` (after
  `mcp.request_timeout_seconds`, on a worker thread that is then
  abandoned) or `internal_error` (a panic).
- Each tool call loads the config and opens the sources afresh, like a
  CLI invocation.

## Verification

`crates/ragmonk-cli/tests/mcp.rs` indexes the fixture code+docs corpus,
then drives `ragmonk serve --mcp` through a session covering the
protocol, validation errors, and each tool's success and error paths, and
compares normalized responses with `fixtures/expected/mcp.json`
(`RAGMONK_BLESS=1` regenerates it).

- Normalization: corpus, home and source id are masked, opaque ids are
  dropped, and volatile values (timestamps, pids, sizes) are masked.
- Arrays with tied ranks are sorted.
