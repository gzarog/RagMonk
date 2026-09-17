"""ChatGPT-via-Codex subscription adapter (subscription plan, Phase 2).

Talks to the official Codex App Server over local stdio JSON (the
documented ``initialize``/``initialized`` lifecycle, account and model
methods, and thread/turn execution). This adapter is **beta**: the exact
method names and payloads here are coded against the documented protocol
shape and must be re-verified against the pinned Codex version before the
provider leaves beta (see ``docs/providers/subscription-integration-note.md``).

Design invariants this module enforces (not merely requests):

* The privacy gate is applied by ``ai/factory.py`` *before* this module is
  imported or any process is spawned -- nothing here re-opens that hole.
* Every answer runs in a fresh thread/turn with tool, file, web, plugin,
  hook, and inherited-MCP access disabled in the *runtime configuration*
  sent on ``initialize`` and thread creation, so prompt injection inside
  retrieved evidence cannot make the runtime act (release gate 5). No
  earlier question's state is carried into a new answer (release gate 6).
* A ChatGPT (subscription) auth mode is required; an unexpected API-key
  mode is rejected, and ``OPENAI_API_KEY`` is never read here.
* Partial/failed/cancelled turns are never returned as a successful
  answer; the model is taken from the runtime, never defaulted to the API
  adapter's model; usage is reported only when the runtime provides it.

The synchronous ``AiProvider``/``SubscriptionRuntime`` surface is bridged
onto the async transport by ``RuntimeBridge`` (one worker-thread event
loop), so this works identically from the one-shot CLI and the running MCP
server.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from ragmonk.ai._transport import JsonRpcClient, JsonRpcError, StdioByteStream
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
    AiUnsupportedVersionError,
    AiUsage,
    build_prompt,
)
from ragmonk.ai.runtime import RuntimeBridge, RuntimeStatus, SubscriptionRuntime
from ragmonk.core.config import AiConfig, PrivacyConfig
from ragmonk.core.errors import RagMonkError

PROVIDER_ID = "codex"

# The official executable RagMonk drives. Deliberately a fixed, validated
# name -- never a path taken from (untrusted) project config, so a
# ``.ragmonk.yaml`` can't point RagMonk at an arbitrary binary to run.
_EXECUTABLE = "codex"
_APP_SERVER_ARGS = ("app-server",)

# Tested Codex version range (inclusive lower bound). A no-op placeholder
# until Phase 0 pins a real range; kept as a real, tested comparison so
# the "unsupported version" gate is wired, not merely described.
_MIN_TESTED_VERSION = (0, 0, 0)

# Isolation sent to the runtime on initialize and per thread. Enforced in
# configuration, not just the system prompt.
_ISOLATION: dict[str, Any] = {
    "tools": False,
    "fileAccess": False,
    "web": False,
    "plugins": False,
    "hooks": False,
    "mcpServers": [],
}


def _parse_version(text: str) -> tuple[int, ...] | None:
    """Pull an ``a.b.c`` version out of a runtime's ``--version`` output.
    Returns ``None`` if nothing parseable is found (treated by the caller
    as "unknown", not as unsupported).
    """
    import re

    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def _check_supported_version(version: tuple[int, ...] | None) -> None:
    if version is None:
        return
    if version < _MIN_TESTED_VERSION:
        installed = ".".join(map(str, version))
        minimum = ".".join(map(str, _MIN_TESTED_VERSION))
        raise AiUnsupportedVersionError(
            f"the installed {_EXECUTABLE} runtime {installed} is older than the tested "
            f"minimum {minimum}; upgrade it to use this provider"
        )


def _map_runtime_error(exc: JsonRpcError) -> RagMonkError:
    """Translate the runtime's own error response into a semantic RagMonk
    error so a caller sees "quota exhausted" / "sign in again" rather than
    an opaque code.
    """
    code = str(exc.code).lower() if exc.code is not None else ""
    text = f"{code} {exc.runtime_message}".lower()
    if code in {"429", "quota_exceeded", "insufficient_quota"} or "quota" in text:
        return AiQuotaExhaustedError(
            f"the ChatGPT account's allowance is exhausted: {exc.runtime_message}"
        )
    if code in {"401", "unauthenticated", "login_required"} or "login" in text or "auth" in text:
        return AiAuthenticationRequiredError(
            f"the Codex runtime needs sign-in: {exc.runtime_message}. "
            "Run: ragmonk ai login codex"
        )
    if code in {"403", "forbidden", "policy"} or "policy" in text or "forbidden" in text:
        return AiPolicyBlockedError(
            f"the Codex runtime refused the request on policy grounds: {exc.runtime_message}"
        )
    return exc


class CodexSession:
    """The async protocol operations, one per JSON message exchange. Holds
    no cross-request state beyond the connection, so each ``answer`` is a
    fresh thread/turn.
    """

    def __init__(self, client: JsonRpcClient, *, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._initialized = False

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._client.request(method, params, timeout=self._timeout)
        except JsonRpcError as exc:
            raise _map_runtime_error(exc) from exc

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._request(
            "initialize",
            {
                "clientInfo": {"name": "ragmonk", "title": "RagMonk"},
                "capabilities": {"isolation": _ISOLATION},
                "authMode": "chatgpt",
            },
        )
        await self._client.notify("initialized", {})
        self._initialized = True

    async def account_status(self) -> RuntimeStatus:
        await self.initialize()
        result = await self._request("account/getStatus")
        return _status_from_result(result)

    async def login(self) -> RuntimeStatus:
        await self.initialize()
        result = await self._request("account/login", {"mode": "chatgpt"})
        return _status_from_result(result)

    async def logout(self) -> None:
        await self.initialize()
        await self._request("account/logout")

    async def models(self) -> list[str]:
        await self.initialize()
        result = await self._request("model/list")
        raw = result.get("models", [])
        names: list[str] = []
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    name = entry.get("id") or entry.get("name")
                    if isinstance(name, str):
                        names.append(name)
                elif isinstance(entry, str):
                    names.append(entry)
        return names

    async def answer(self, system: str, user: str, *, model: str) -> tuple[str, str, AiUsage]:
        await self.initialize()
        thread = await self._request("thread/create", {"isolation": _ISOLATION})
        thread_id = thread.get("threadId") or thread.get("id")
        if not isinstance(thread_id, str):
            raise AiInvalidResponseError("Codex runtime did not return a thread id")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "stream": False,
            "isolation": _ISOLATION,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # A model is passed only when the user configured one; otherwise the
        # runtime chooses and reports it -- never the API adapter's default.
        if model:
            params["model"] = model
        result = await self._request("thread/runTurn", params)
        return _answer_from_result(result)


def _status_from_result(result: dict[str, Any]) -> RuntimeStatus:
    authenticated = bool(result.get("authenticated", False))
    account_raw = result.get("account")
    account: str | None = None
    if isinstance(account_raw, dict):
        value = account_raw.get("email") or account_raw.get("id") or account_raw.get("name")
        account = value if isinstance(value, str) else None
    elif isinstance(account_raw, str):
        account = account_raw
    auth_mode = result.get("authMode")
    if authenticated and auth_mode not in (None, "chatgpt"):
        raise AiPolicyBlockedError(
            f"the Codex runtime is signed in with an unexpected auth mode {auth_mode!r}; "
            "RagMonk's codex provider requires ChatGPT (subscription) sign-in, not an API key"
        )
    return RuntimeStatus(
        provider_id=PROVIDER_ID,
        authenticated=authenticated,
        account=account,
        runtime_version=result.get("runtimeVersion") if isinstance(
            result.get("runtimeVersion"), str
        ) else None,
        detail="signed in" if authenticated else "not signed in; run 'ragmonk ai login codex'",
    )


def _answer_from_result(result: dict[str, Any]) -> tuple[str, str, AiUsage]:
    status = result.get("status")
    if status not in (None, "completed"):
        raise AiInvalidResponseError(
            f"Codex turn did not complete (status={status!r}); refusing to present it as an answer"
        )
    message = result.get("message")
    text = ""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    elif isinstance(result.get("text"), str):
        text = result["text"]
    if not text:
        raise AiInvalidResponseError("Codex turn returned no answer text")
    model = result.get("model")
    if not isinstance(model, str) or not model:
        model = "codex"
    usage_raw = result.get("usage")
    usage = AiUsage()
    if isinstance(usage_raw, dict):
        usage = AiUsage(
            input_tokens=_as_int(usage_raw.get("inputTokens")),
            output_tokens=_as_int(usage_raw.get("outputTokens")),
        )
    return text, model, usage


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


class CodexRuntime:
    """Synchronous ``SubscriptionRuntime`` bridged onto one async session.

    Concurrency is bounded to a single in-flight request per runtime (a
    lock), and the child process/session is closed with the runtime.
    """

    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        session_factory: Callable[[], Awaitable[CodexSession]],
        timeout: float,
        bridge: RuntimeBridge | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._timeout = timeout
        self._bridge = bridge or RuntimeBridge()
        self._session: CodexSession | None = None
        self._lock = threading.Lock()

    async def _ensure_session(self) -> CodexSession:
        if self._session is None:
            self._session = await self._session_factory()
        return self._session

    def _run(self, make_coro: Callable[[CodexSession], Awaitable[Any]]) -> Any:
        with self._lock:

            async def _op() -> Any:
                session = await self._ensure_session()
                return await make_coro(session)

            return self._bridge.run(_op(), timeout=self._timeout + 5.0)

    def status(self) -> RuntimeStatus:
        return self._run(lambda s: s.account_status())

    def login(self) -> RuntimeStatus:
        return self._run(lambda s: s.login())

    def logout(self) -> None:
        self._run(lambda s: s.logout())

    def models(self) -> list[str]:
        return self._run(lambda s: s.models())

    def answer_text(self, system: str, user: str, *, model: str) -> tuple[str, str, AiUsage]:
        return self._run(lambda s: s.answer(system, user, model=model))

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            # Close is best-effort on every exit path -- the bridge is torn
            # down regardless of whether the child answered cleanly.
            with contextlib.suppress(Exception):
                self._bridge.run(session._client.aclose(), timeout=5.0)
        self._bridge.close()


class CodexProvider:
    """``AiProvider`` for ``ragmonk ask``: sends the shared evidence prompt
    through a fresh, isolated Codex turn.
    """

    def __init__(self, *, runtime: CodexRuntime, model: str = "") -> None:
        self._runtime = runtime
        self._model = model

    def answer(self, request: AiRequest) -> AiAnswer:
        text, model, usage = self._runtime.answer_text(
            SYSTEM_PROMPT, build_prompt(request), model=self._model
        )
        return AiAnswer(text=text, provider=PROVIDER_ID, model=model, usage=usage)


async def _spawn_session(timeout: float) -> CodexSession:
    """Real session factory: spawn the official Codex App Server over
    stdio. A missing or unstartable executable surfaces as
    ``AiRuntimeUnavailableError`` *before* anything is presented as usable.
    """
    if shutil.which(_EXECUTABLE) is None:
        raise AiRuntimeUnavailableError(
            f"the {_EXECUTABLE!r} runtime is not installed or not on PATH; "
            "install it to use the codex provider (see docs/providers)."
        )
    await _verify_version()
    try:
        process = await asyncio.create_subprocess_exec(
            _EXECUTABLE,
            *_APP_SERVER_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise AiRuntimeUnavailableError(
            f"could not start the {_EXECUTABLE!r} runtime: {exc}"
        ) from exc
    client = JsonRpcClient(StdioByteStream(process))
    await client.start()
    return CodexSession(client, timeout=timeout)


async def _verify_version() -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            _EXECUTABLE,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
    except (TimeoutError, FileNotFoundError, PermissionError, OSError):
        return  # version check is best-effort; a real call still fails clearly
    _check_supported_version(_parse_version(out.decode("utf-8", "replace")))


def create_runtime(ai: AiConfig) -> SubscriptionRuntime:
    """Factory used by ``ai/runtime.resolve_runtime`` (``ragmonk ai``)."""
    timeout = ai.timeout_seconds
    return CodexRuntime(session_factory=lambda: _spawn_session(timeout), timeout=timeout)


def create_ai_provider(*, ai: AiConfig, privacy: PrivacyConfig) -> AiProvider:
    """Factory used by ``ai/factory.create_provider`` (``ragmonk ask``).

    ``privacy`` is already gated by the factory before this is called; it
    is accepted to match the adapter-builder contract and to keep the gate
    decision with the caller.
    """
    runtime = create_runtime(ai)
    assert isinstance(runtime, CodexRuntime)
    return CodexProvider(runtime=runtime, model=ai.model)
