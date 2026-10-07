# ADR 0026: AI providers, `ask` and `ai` (RUST-14, slice 1)

Status: accepted

## Decisions (user)

1. RUST-14 ships in two slices: AI providers first (this ADR), the Admin
   UI second.
2. Subscription providers:
   - **Codex** is driven over its stdio JSON-RPC app server, as in the
     reference.
   - **GitHub Copilot** is driven through the signed-in `copilot` CLI.
     The reference's Python SDK has no Rust counterpart.
3. Parity is proven with golden HTTP and JSON-RPC exchanges recorded
   from the reference against local mocks.
4. The Admin UI (slice 2) will use axum, tokio and minijinja.

## Crate `ragmonk-ai`

- **`prompt`:** the reference's system prompt and evidence prompt, byte
  for byte.
  - Values are interpolated with Python `str()` semantics (`None`,
    `True`, dict reprs).
  - Evidence locations keep the reference's key order.
- **`http`:** blocking `reqwest` clients for OpenAI, OpenAI-compatible
  endpoints, Anthropic and Ollama.
  - The request bodies, paths and auth headers are the reference's.
  - Responses map the same way, including fallbacks to the configured
    model and null usage.
  - There are no retries.
  - Failure messages match what the reference's SDKs produce:
    - `Connection error.`
    - `Request timed out.`
    - `Error code: <status> - <repr of the JSON body>`, with key order
      kept.
    - The raw text for a non-JSON error body.
    - Python's `json` messages for a 2xx body that is not JSON.
- **`factory`:**
  - Provider selection, with the reference's configuration errors.
  - The `privacy.external_ai_allowed` gate runs before anything is
    constructed.
  - Ollama is exempt only when its host is loopback.
  - API keys come from the same environment variables.
- **`registry`:** the capability table that `ai providers` prints.
- **`transport`:** a bounded, id-correlated JSON-RPC client over
  newline-delimited JSON.
  - It reassembles split messages.
  - It ignores notifications and replies to unknown ids.
  - Malformed JSON, EOF and unframed messages over 8 MiB fail as invalid
    responses.
  - Each request has a deadline.
- **`codex`:** the reference's `codex app-server` protocol.
  - Messages: `initialize` with isolation, then `account/*`,
    `model/list`, `thread/create` and `thread/runTurn`.
  - Errors map to quota, sign-in and policy errors.
  - API-key auth modes are refused.
  - Incomplete or empty turns are never answers.
  - The executable is the fixed name `codex` found on `PATH`, never a
    configured path.
- **`copilot`:** runs `copilot -p PROMPT [--model M]`.
  - Token variables (`GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_GITHUB_TOKEN`,
    and the API keys) are removed from the child's environment, so only
    the CLI's own sign-in is used.
  - No tool is approved.
  - Failures map with the reference's text rules.
  - Sign-in and sign-out are left to the CLI (`/login`, `/logout`). The
    CLI reports no usage and no model list.
  - **Divergence:** the reference's SDK adapter was a placeholder that
    always reported the runtime unavailable; this one answers.

Errors carry the reference's class names through a new
`RagMonkError::with_class` (for example `AiNotConfiguredError`). Their
exit codes are those of the base classes: config 3, policy 8, runtime 1.

## CLI and MCP

- **`ragmonk ask QUESTION [--json]`:**
  - Runs `explore`'s retrieval, then builds the provider, then asks it.
  - The JSON payload and text rendering are the reference's.
- **`ragmonk ai providers|status|login|logout|models`:**
  - Same validation, privacy gate (not on `logout`), output and errors
    as the reference.
- **MCP:** `ragmonk_ask` is now served, reversing ADR 0025's omission.
  - The tool catalog and `initialize` instructions are the reference's,
    unmodified.
  - Errors are typed by class name (for example `AiNotConfiguredError`).

## Verification

- **`compat/tools/gen_ai_golden.py` → `compat/golden/ai.json`:**
  - The prompt.
  - 34 provider cases: success, empty evidence, 401 JSON, 500 text,
    non-JSON, bare responses and connection refused, for each provider.
    Each records the request sent and the outcome.
  - 16 factory cases.
  - 17 Codex exchanges against a scripted runtime that interleaves
    notifications and unknown ids, and splits replies across writes.
    These record every message sent.
  - Replayed by `crates/ragmonk-ai/tests/golden.rs` against a TCP mock
    and an OS-pipe fake runtime.
  - The OS text behind a refused Ollama connection is platform-specific,
    so only its prefix is compared.
- **`compat/tools/gen_ai_cli_golden.py` → `compat/golden/ai_cli.json`:**
  - `ask` (JSON, text, no evidence, HTTP 500, not configured, privacy)
    and the `ai` lifecycle commands, on the compat corpus with a mock
    Ollama and no `codex` on `PATH`.
  - Replayed by `crates/ragmonk-cli/tests/ai.rs`: exit codes, stdout and
    the provider request match; stderr matches up to the reference's line
    wrapping.
- **MCP:** the session golden now includes `ragmonk_ask` calls.
