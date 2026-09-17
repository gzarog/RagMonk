# Subscription AI providers (beta)

RagMonk can answer `ragmonk ask` questions using an eligible **existing AI
subscription** instead of an API key, through each provider's own official
runtime. This is a convenience over key management — it is still cloud
inference that consumes your account's allowance, and it is neither offline
nor unlimited. Indexing and retrieval stay fully local; enabling a
subscription provider requires no reindexing.

Two subscription providers ship, both **beta**:

| Provider id | Product | Runtime | Optional install |
|---|---|---|---|
| `codex` | ChatGPT (Codex access) | official Codex runtime (`codex`) | install the Codex runtime and put it on `PATH` |
| `github_copilot` | GitHub Copilot | official Copilot Python SDK | `pip install "ragmonk[copilot]"` |

## Two ways to use a subscription

### A. From your existing AI client, over MCP (no RagMonk provider)

Register RagMonk as an MCP server in a client you already sign into, then
ask that client to retrieve with `ragmonk_explore` and answer from the
returned evidence:

```sh
ragmonk install-agent --client codex
# or:
ragmonk install-agent --client claude-code
```

RagMonk needs no AI provider configured for this flow. Note the privacy
boundary: RagMonk's retrieval reads locally, but the cloud client sends the
tool results to its own provider — RagMonk's `privacy.external_ai_allowed`
flag does not govern that external client's inference.

### B. From `ragmonk ask`, using a subscription provider

```sh
ragmonk ai providers                 # list providers and capabilities
ragmonk ai login codex               # official sign-in (browser/device)
ragmonk ai status codex              # connection state, no secrets
ragmonk ai models codex              # models the account offers
ragmonk config set privacy.external_ai_allowed true
ragmonk config set ai.provider codex
ragmonk ask "Explain the settlement flow and cite the source files"
ragmonk ai logout codex
```

Model selection: leave `ai.model` empty to let the runtime choose and
report the model; set it (`ragmonk config set ai.model <name>`) to pin one
from `ragmonk ai models`. The OpenAI API adapter's default model is never
used for a subscription provider.

Switching providers is just `ragmonk config set ai.provider <id>`; the API
(`openai`/`anthropic`/`openai_compatible`) and local (`ollama`) providers
remain available and are never silently substituted.

## Configuration

```yaml
ai:
  provider: codex        # or github_copilot, or an existing provider
  model: ""              # empty = let the runtime choose and report it
  timeout_seconds: 120
  codex:
    auth_mode: chatgpt
  github_copilot:
    auth_mode: signed_in_user
privacy:
  external_ai_allowed: true
```

No secrets belong in this file. Sign-in is delegated to the provider's
runtime; RagMonk stores no token and reads none from config. The runtime
executable is a fixed, validated name — a project-level `.ragmonk.yaml`
cannot point RagMonk at an arbitrary binary to run.

## What RagMonk does and does not do

- **Never** asks for a password, copies browser cookies, or extracts a
  token.
- **Never** silently uses an API key or a billable fallback for a
  subscription request; it fails clearly when sign-in is required.
- **Never** lets `GH_TOKEN`/`GITHUB_TOKEN`/`OPENAI_API_KEY` quietly become
  the effective sign-in; the effective auth mode is verified and an
  unexpected token-based mode is rejected. `ragmonk doctor` flags such env
  vars.
- Runs every answer in a **fresh, isolated session** with tools, file
  access, web, plugins, hooks, and inherited MCP servers disabled in the
  runtime configuration — so prompt injection inside retrieved evidence
  cannot make the runtime act, and no earlier question's state leaks in.
- Blocks a cloud provider **before** starting any child process when
  `privacy.external_ai_allowed` is false.
- Reports usage only when the runtime provides it; it never invents a
  dollar cost from token counts.

## Diagnostics

`ragmonk doctor` gains an **AI** section: it reports the configured
provider and privacy posture and, for a subscription provider, whether its
runtime/SDK is even detectable and whether any shadowing credential env var
is present — all offline and without printing secrets.

## Limitations and status

- Both adapters are **beta** and gated behind version/isolation checks (see
  [`subscription-integration-note.md`](subscription-integration-note.md)).
  If answer-only isolation cannot be enforced for your pinned runtime
  version, use the MCP client flow (A) instead.
- Account allowances, model access, and provider/organization policies
  still apply.
- Subscription mode is not an unlimited background service: daemon indexing
  never uses it and never prompts for sign-in.
- Tested runtime/SDK versions are recorded in the integration note as real
  verification is performed; a successful login alone is not a
  compatibility claim.

## Claude and Gemini

For Claude, use the MCP client flow (A) with Claude Code; RagMonk ships no
Claude subscription-token adapter. Gemini is a later feasibility item. See
the integration note for details.
