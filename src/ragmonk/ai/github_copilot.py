"""GitHub Copilot subscription adapter (subscription plan, Phase 3).

Uses the official GitHub Copilot Python SDK, which drives the signed-in
Copilot CLI credentials and consumes the account's Copilot allowance (not
a separate unlimited entitlement). Like the Codex adapter this is **beta**:
the exact SDK symbol/method names used by ``_load_client`` are coded
against the documented lifecycle and must be re-verified against the pinned
SDK version before the provider leaves beta (see
``docs/providers/subscription-integration-note.md``). All behavior below is
covered by tests against a fake client, so none of the mapping logic
depends on the SDK actually being installed.

Design invariants this module enforces (not merely requests):

* The privacy gate is applied by ``ai/factory.py`` before this module is
  imported or the SDK is loaded.
* The effective authentication must be a signed-in Copilot user. RagMonk
  never collects a GitHub password or extracts a token, and an effective
  mode that looks token-based (an inherited ``GH_TOKEN``/``GITHUB_TOKEN``
  or an API key rather than the CLI sign-in) is rejected rather than used
  silently.
* Each answer runs with tools/file/web/plugins/hooks/inherited-MCP access
  disabled in the request configuration; the model comes from the SDK,
  never an API adapter's default; partial/failed completions are never
  presented as answers; usage is reported only when the SDK provides it,
  and a dollar cost is never invented from token counts.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ragmonk.ai.base import _SYSTEM_PROMPT as SYSTEM_PROMPT
from ragmonk.ai.base import (
    AiAnswer,
    AiAuthenticationRequiredError,
    AiInvalidResponseError,
    AiPolicyBlockedError,
    AiProvider,
    AiQuotaExhaustedError,
    AiRequest,
    AiRuntimeUnavailableError,
    AiUsage,
    build_prompt,
)
from ragmonk.ai.runtime import RuntimeBridge, RuntimeStatus, SubscriptionRuntime
from ragmonk.core.config import AiConfig, PrivacyConfig
from ragmonk.core.errors import RagMonkError

PROVIDER_ID = "github_copilot"

# The SDK module RagMonk loads at call time. Kept as a constant (not an
# arbitrary, config-controlled import) and imported lazily so this module
# is importable -- and testable against a fake client -- without the SDK
# installed. The exact distribution/import name must be confirmed against
# the pinned SDK version (Phase 0) before leaving beta.
_SDK_IMPORT_NAME = "copilot"

# Environment variables whose presence must NOT be silently treated as the
# effective Copilot sign-in. Their existence alone is not fatal (the SDK
# may ignore them), but the runtime verifies the effective auth mode and
# refuses a token-based mode.
_CONFLICTING_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

_ISOLATION: dict[str, Any] = {
    "tools": False,
    "fileAccess": False,
    "web": False,
    "plugins": False,
    "hooks": False,
    "mcpServers": [],
}

_ALLOWED_AUTH_MODES = frozenset({"signed_in_user", "oauth", "cli", None})


class CopilotClient(Protocol):
    """The minimal async surface RagMonk needs from the Copilot SDK. The
    real client (``_load_client``) adapts the SDK onto this; tests provide
    a fake implementing exactly these methods.
    """

    async def account_status(self) -> dict[str, Any]: ...

    async def login(self) -> dict[str, Any]: ...

    async def logout(self) -> None: ...

    async def list_models(self) -> list[str]: ...

    async def complete(
        self, *, system: str, user: str, model: str, isolation: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


def _map_client_error(exc: Exception) -> RagMonkError:
    """Best-effort translation of an SDK exception into a semantic RagMonk
    error. Since the SDK's own exception taxonomy is not pinned yet, this
    matches on the message text and falls back to a generic invalid-response
    rather than guessing a specific class.
    """
    text = str(exc).lower()
    if "quota" in text or "rate limit" in text or "429" in text:
        return AiQuotaExhaustedError(f"the Copilot account's allowance is exhausted: {exc}")
    if "unauth" in text or "login" in text or "sign in" in text or "401" in text:
        return AiAuthenticationRequiredError(
            f"the Copilot SDK needs sign-in: {exc}. Run: ragmonk ai login github_copilot"
        )
    if "forbidden" in text or "policy" in text or "organization" in text or "403" in text:
        return AiPolicyBlockedError(f"Copilot refused the request on policy grounds: {exc}")
    return AiInvalidResponseError(f"the Copilot SDK returned an unusable result: {exc}")


def _verify_auth_mode(result: dict[str, Any]) -> None:
    mode = result.get("authMode")
    if mode not in _ALLOWED_AUTH_MODES:
        raise AiPolicyBlockedError(
            f"the Copilot SDK reports an unexpected auth mode {mode!r}; RagMonk's "
            "github_copilot provider requires a signed-in Copilot user, not a token/API key. "
            f"Check that none of {', '.join(_CONFLICTING_ENV)} is overriding the CLI sign-in."
        )


def _status_from_result(result: dict[str, Any]) -> RuntimeStatus:
    _verify_auth_mode(result)
    authenticated = bool(result.get("authenticated", False))
    account_raw = result.get("account")
    account: str | None = None
    if isinstance(account_raw, dict):
        value = account_raw.get("login") or account_raw.get("email") or account_raw.get("name")
        account = value if isinstance(value, str) else None
    elif isinstance(account_raw, str):
        account = account_raw
    return RuntimeStatus(
        provider_id=PROVIDER_ID,
        authenticated=authenticated,
        account=account,
        runtime_version=result.get("sdkVersion")
        if isinstance(result.get("sdkVersion"), str)
        else None,
        detail="signed in"
        if authenticated
        else "not signed in; run 'ragmonk ai login github_copilot'",
    )


def _answer_from_result(result: dict[str, Any]) -> tuple[str, str, AiUsage]:
    status = result.get("status")
    if status not in (None, "completed", "success"):
        raise AiInvalidResponseError(
            f"Copilot completion did not finish (status={status!r}); "
            "refusing to present it as an answer"
        )
    text = result.get("text")
    if not isinstance(text, str) or not text:
        message = result.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            text = message["content"]
    if not isinstance(text, str) or not text:
        raise AiInvalidResponseError("Copilot completion returned no answer text")
    model = result.get("model")
    if not isinstance(model, str) or not model:
        model = "github_copilot"
    # Usage stays unknown unless the SDK actually reports token counts; a
    # dollar cost is never derived from them.
    usage = AiUsage()
    usage_raw = result.get("usage")
    if isinstance(usage_raw, dict):
        usage = AiUsage(
            input_tokens=_as_int(usage_raw.get("inputTokens") or usage_raw.get("promptTokens")),
            output_tokens=_as_int(
                usage_raw.get("outputTokens") or usage_raw.get("completionTokens")
            ),
        )
    return text, model, usage


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


class CopilotRuntime:
    """Synchronous ``SubscriptionRuntime`` bridged onto one async client,
    concurrency bounded to a single in-flight request.
    """

    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[CopilotClient]],
        timeout: float,
        bridge: RuntimeBridge | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._timeout = timeout
        self._bridge = bridge or RuntimeBridge()
        self._client: CopilotClient | None = None
        self._lock = threading.Lock()

    async def _ensure_client(self) -> CopilotClient:
        if self._client is None:
            self._client = await self._client_factory()
        return self._client

    def _run(self, make_coro: Callable[[CopilotClient], Awaitable[Any]]) -> Any:
        with self._lock:

            async def _op() -> Any:
                client = await self._ensure_client()
                try:
                    return await make_coro(client)
                except RagMonkError:
                    raise
                except Exception as exc:  # noqa: BLE001 - SDK-specific -> one typed error
                    raise _map_client_error(exc) from exc

            return self._bridge.run(_op(), timeout=self._timeout + 5.0)

    def status(self) -> RuntimeStatus:
        return self._run(lambda c: self._status(c))

    async def _status(self, client: CopilotClient) -> RuntimeStatus:
        return _status_from_result(await client.account_status())

    def login(self) -> RuntimeStatus:
        return self._run(lambda c: self._login(c))

    async def _login(self, client: CopilotClient) -> RuntimeStatus:
        return _status_from_result(await client.login())

    def logout(self) -> None:
        self._run(lambda c: c.logout())

    def models(self) -> list[str]:
        return self._run(lambda c: c.list_models())

    def answer_text(self, system: str, user: str, *, model: str) -> tuple[str, str, AiUsage]:
        return self._run(lambda c: self._answer(c, system, user, model))

    async def _answer(
        self, client: CopilotClient, system: str, user: str, model: str
    ) -> tuple[str, str, AiUsage]:
        result = await client.complete(
            system=system, user=user, model=model, isolation=dict(_ISOLATION)
        )
        return _answer_from_result(result)

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                self._bridge.run(client.aclose(), timeout=5.0)
        self._bridge.close()


class CopilotProvider:
    """``AiProvider`` for ``ragmonk ask``."""

    def __init__(self, *, runtime: CopilotRuntime, model: str = "") -> None:
        self._runtime = runtime
        self._model = model

    def answer(self, request: AiRequest) -> AiAnswer:
        text, model, usage = self._runtime.answer_text(
            SYSTEM_PROMPT, build_prompt(request), model=self._model
        )
        return AiAnswer(text=text, provider=PROVIDER_ID, model=model, usage=usage)


async def _load_client(timeout: float) -> CopilotClient:
    """Real client factory: import the official Copilot SDK and adapt it
    onto ``CopilotClient``. A missing SDK surfaces as
    ``AiRuntimeUnavailableError`` before anything looks usable.

    The SDK adapter itself is intentionally thin and marked beta: the
    concrete SDK entry points must be confirmed against the pinned version
    (Phase 0). Until then this raises a clear, actionable error rather than
    guessing an API that might not exist.
    """
    import importlib

    try:
        importlib.import_module(_SDK_IMPORT_NAME)
    except ImportError as exc:
        raise AiRuntimeUnavailableError(
            f"the GitHub Copilot SDK ({_SDK_IMPORT_NAME!r}) is not installed; "
            "install the optional dependency to use this provider "
            '(pip install "ragmonk[copilot]") and sign in with the Copilot CLI.'
        ) from exc
    raise AiRuntimeUnavailableError(
        "the GitHub Copilot adapter is beta: its SDK bindings must be pinned and verified "
        "before it can run (see docs/providers/subscription-integration-note.md). "
        "Use the MCP client flow in the meantime."
    )


def create_runtime(ai: AiConfig) -> SubscriptionRuntime:
    """Factory used by ``ai/runtime.resolve_runtime`` (``ragmonk ai``)."""
    timeout = ai.timeout_seconds
    return CopilotRuntime(client_factory=lambda: _load_client(timeout), timeout=timeout)


def create_ai_provider(*, ai: AiConfig, privacy: PrivacyConfig) -> AiProvider:
    """Factory used by ``ai/factory.create_provider`` (``ragmonk ask``).

    ``privacy`` is already gated by the factory before this is called.
    """
    runtime = create_runtime(ai)
    assert isinstance(runtime, CopilotRuntime)
    return CopilotProvider(runtime=runtime, model=ai.model)


def has_conflicting_env(environ: dict[str, str] | None = None) -> list[str]:
    """Names of credential env vars present that could shadow the intended
    CLI sign-in. Surfaced by diagnostics (Phase 4); not fatal on its own.
    """
    source = os.environ if environ is None else environ
    return [name for name in _CONFLICTING_ENV if source.get(name)]
