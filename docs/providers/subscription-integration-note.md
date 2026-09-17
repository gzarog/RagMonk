# Subscription AI providers — Phase 0 integration note

Status: **planning / feasibility record**. This document is the Phase 0
deliverable of the subscription AI provider plan. It records the
integration contract each proposed subscription adapter must satisfy
*before* any adapter code is trusted, and the go/no-go framework used to
decide whether an adapter ships enabled, ships beta, or stays in
external-client (MCP) mode only.

It does **not** claim live compatibility. A successful account login is
not evidence that answer-only, evidence-grounded execution works. Every
claim below must be re-verified against the pinned runtime/SDK version at
the time an adapter is actually implemented, because provider availability
and integration policies change.

## 1. What Phase 0 must establish

For each candidate subscription provider (`codex`, `github_copilot`):

1. **Pinned versions.** The exact runtime/SDK version tested, and the
   OS/architecture combinations it was verified on. A version range is
   recorded only after the low and high ends of the range are both
   exercised.
2. **Authentication mode.** How an account signs in without an API key,
   how the session is refreshed, and how RagMonk detects the difference
   between "signed in with a subscription account" and "falling back to
   an API key". A subscription credential is never a substitute value for
   `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, and the adapter must reject an
   unexpected API-key mode rather than silently use it.
3. **Isolation.** How the runtime is told to disable shell access, file
   access, web tools, plugins, hooks, and any inherited MCP servers for an
   answer-only turn. Isolation is enforced in runtime configuration, not
   merely requested in a system prompt.
4. **Sync/async boundary.** How the synchronous `AiProvider.answer()`
   contract (see `src/ragmonk/ai/base.py`) drives an async runtime call,
   including when invoked from the already-running event loop of the MCP
   server. The resolution must not call `asyncio.run()` inside a running
   loop; where necessary a dedicated runtime worker/event-loop thread
   bridges the two.
5. **Go/no-go.** A recorded decision. If answer-only execution cannot be
   enforced for a provider, that provider stays in external-client MCP
   mode and no `ragmonk ask` adapter ships for it.

## 2. The integration contract every adapter must meet

These are the invariants later phases build against. They are stated here
so the adapter code in Phases 2–3 has a fixed target and the fake-runtime
contract tests (release gate 4) have something concrete to assert.

### 2.1 Credential and fallback safety

- A subscription provider must **fail clearly** when sign-in is required.
  Normal `ask`, daemon, and MCP requests must not open a browser
  unexpectedly.
- A subscription request must never silently use an API key or switch to
  a billable API fallback. The effective authentication mode/account is
  verified before the request is sent.
- Inherited environment credentials (`GH_TOKEN`, `GITHUB_TOKEN`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...) must not take precedence
  over, or be confused with, the intended subscription sign-in.

### 2.2 Privacy gate ordering

- `privacy.external_ai_allowed=false` blocks a cloud-backed adapter
  **before** any child process is created, even when the transport is
  local stdio. The gate is applied in `ai/factory.py` in the same place
  the existing cloud providers are gated (see
  `_require_external_ai_allowed`).

### 2.3 Isolation and injection resistance

- Prompt injection inside retrieved evidence must not be able to trigger
  tools, read other files, load inherited plugins, or recursively call
  `ragmonk_ask`. This is enforced in runtime configuration, not only in a
  system prompt.
- A new answer must carry no earlier user's or question's conversation
  state: each answer runs in a fresh, isolated session.

### 2.4 Response fidelity

- Partial events, completion failure, cancellation, and process exit must
  never be presented as a successful answer.
- Model identity is discovered dynamically from the runtime and reported
  in `AiAnswer.model`; an adapter never substitutes the API-key adapter's
  default model automatically.
- Usage is reported when the runtime provides it and left unknown
  (`None`) otherwise; a dollar cost is never invented from subscription
  token counts.

### 2.5 Lifecycle and cleanup

- Concurrent requests per runtime are bounded (initially one) and the
  runtime is closed when the owning command or server closes.
- Child processes are reaped on every exit path (success, error,
  cancellation, timeout).
- Temporary evidence is removed on success, error, and cancellation.
- Credentials, full prompts, and authentication URLs containing secrets
  never appear in logs or `config show`.

## 3. Error taxonomy

Phase 1 introduces typed errors so every failure mode below is reported
distinctly (both to a human and, via `--json`, machine-readably), while
retaining the existing exit-code conventions:

| Condition | Meaning |
|---|---|
| authentication required | no usable sign-in; direct the user to official login |
| unavailable runtime | the runtime/SDK is not installed |
| unsupported version | the installed runtime/SDK is outside the tested range |
| quota exhausted | the account's allowance is spent |
| policy blocked | `privacy.external_ai_allowed=false`, or a workspace/org restriction |
| invalid response | malformed, partial-as-final, or wrong-request-id output |
| timeout | the runtime did not answer within the deadline |

## 4. Sync/async bridge decision

`AiProvider.answer()` is synchronous and is called from two places:

- one-shot CLI (`cli/ask.py`), where there is no running event loop, and
- the MCP server (`mcp/`), which already runs inside an asyncio loop.

The adopted resolution (implemented in Phase 1's `ai/runtime.py`): a
single dedicated worker thread owns the runtime's event loop for the
lifetime of the runtime object. `answer()` submits a coroutine to that
loop with a deadline and blocks on the result via a thread-safe future.
This works identically whether or not the caller already has a running
loop, so no code path ever calls `asyncio.run()` re-entrantly. Cancellation
and timeout cancel the submitted coroutine and tear the child process
down.

## 5. Per-adapter go/no-go framework

An adapter is promoted from "external-client MCP mode only" to a shipped
`ragmonk ask` provider only when **all** of the following are recorded for
its pinned version:

1. Account login and answer generation succeed with no API key present.
2. A quota/allowance failure is observed and mapped to the `quota
   exhausted` error, not a generic failure.
3. Isolation controls (§2.3) are confirmed to actually disable tools,
   file access, and inherited MCP servers — verified by an injection
   attempt in evidence that must fail to act.
4. The sync/async bridge (§4) returns a correct answer from both the CLI
   and a running MCP server.
5. Cleanup (§2.5) is confirmed on success, error, cancellation, and
   timeout.

If any of 1–5 cannot be met, the provider stays in external-client MCP
mode and ships no embedded adapter. Codex ships **beta** until its pinned
version passes all five; Copilot follows Codex.

## 6. Providers deferred at Phase 0

- **Claude subscription.** Delivered as MCP client instructions only. No
  Claude subscription-token HTTP adapter is planned; an unmodified-client
  integration is considered only after confirming its precise fit with
  Anthropic's stated conditions.
- **Gemini / Google AI Pro-Ultra.** A later feasibility spike must
  separately verify supported embedding, cached-login headless execution,
  quota behavior, tools isolation, and redistribution conditions before an
  adapter is scoped.

## 7. Exit criteria for Phase 0

This note, kept current with the pinned versions, authentication modes,
isolation settings, and a recorded go/no-go per adapter, is the Phase 0
exit artifact. Later phases fill in the version/observation rows as real
verification is performed; no compatibility is claimed on the basis of a
successful login alone.
