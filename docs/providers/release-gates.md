# Subscription providers — release gates status

Maps each release gate from the subscription AI provider plan to where it
is met in the code and tests. "Enforced" means a runtime/config check, not
only a system-prompt request.

| # | Gate | Status | Where |
|---|---|---|---|
| 1 | Existing API/Ollama/retrieval tests still pass | Met | unchanged suites; no retrieval/index/embedding code touched |
| 2 | Subscription requests cannot silently use an API key or a billable fallback | Enforced | `ai/codex.py` (`authMode=chatgpt`, rejects unexpected mode), `ai/github_copilot.py` (`_verify_auth_mode`, `has_conflicting_env`); no `*_API_KEY` read in either adapter |
| 3 | `privacy.external_ai_allowed=false` blocks cloud adapters before subprocess creation | Enforced | `ai/factory.py` gate before adapter import; `cli/ai.py` `_gate_privacy` before `resolve_runtime` |
| 4 | Fake-runtime contract tests: split JSON, stderr noise, wrong ids, oversized output, expired login, quota, timeouts, cancellation | Met | `tests/unit/test_ai_transport.py`, `tests/unit/test_ai_codex.py`, `tests/unit/test_ai_github_copilot.py` |
| 5 | Injected evidence cannot trigger tools/read files/load plugins/recurse `ragmonk_ask` — enforced in runtime config | Enforced | `_ISOLATION` sent on initialize and per turn/completion in both adapters; asserted by tests |
| 6 | A new answer uses no earlier user's/question's state | Enforced | fresh thread/turn per `answer` (codex); per-call `complete` with no carried state (copilot) |
| 7 | Credentials/full prompts/auth URLs never in logs or `config show` | Met | no secret fields in config models; `RuntimeStatus` carries no secrets (asserted); adapters log nothing |
| 8 | Async SDK invocation works from one-shot CLI and running MCP server; children reaped on every exit | Enforced | `ai/runtime.py` `RuntimeBridge` (dedicated loop, no re-entrant `asyncio.run`), tested incl. nested-loop; `StdioByteStream.aclose` terminates/kills/reaps |
| 9 | Live opt-in account tests verify real auth; automated unit tests need no real subscription/key | Partially met | unit tests need neither (all fake-runtime); live opt-in verification is a Phase 0 activity to record per pinned version |
| 10 | Compare cold/warm latency, citation fidelity, evidence adherence vs an existing provider | Deferred | requires a live account; to be recorded in the integration note. No claim that subscription mode improves search quality or speed |

Gates 9 and 10 require a live subscription and are recorded as they are
performed; the adapters stay **beta** until their pinned versions pass all
gates (see `subscription-integration-note.md`).
