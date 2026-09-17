"""GitHub Copilot adapter contract tests (subscription plan, Phase 3).

Driven entirely by a fake ``CopilotClient`` -- no SDK, no GitHub account,
no network. Every mapping decision (auth-mode/token rejection,
quota/expired-login translation, partial-as-final refusal, usage-unknown,
model from the SDK) is the real code under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from ragmonk.ai import github_copilot as gh
from ragmonk.ai.base import (
    AiAuthenticationRequiredError,
    AiInvalidResponseError,
    AiPolicyBlockedError,
    AiQuotaExhaustedError,
    AiRequest,
    AiRuntimeUnavailableError,
)
from ragmonk.ai.github_copilot import CopilotProvider, CopilotRuntime


class FakeCopilotClient:
    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        models: list[str] | None = None,
        completion: dict[str, Any] | None = None,
        raise_on_complete: Exception | None = None,
    ) -> None:
        self._status = status or {
            "authenticated": True,
            "authMode": "signed_in_user",
            "account": {"login": "octocat"},
        }
        self._models = models if models is not None else ["gpt-4o", "o4-mini"]
        self._completion = completion or {
            "status": "completed",
            "model": "gpt-4o",
            "text": "Hello from Copilot",
        }
        self._raise_on_complete = raise_on_complete
        self.last_complete_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def account_status(self) -> dict[str, Any]:
        return self._status

    async def login(self) -> dict[str, Any]:
        return self._status

    async def logout(self) -> None:
        return None

    async def list_models(self) -> list[str]:
        return self._models

    async def complete(
        self, *, system: str, user: str, model: str, isolation: dict[str, Any]
    ) -> dict[str, Any]:
        self.last_complete_kwargs = {
            "system": system,
            "user": user,
            "model": model,
            "isolation": isolation,
        }
        if self._raise_on_complete is not None:
            raise self._raise_on_complete
        return self._completion

    async def aclose(self) -> None:
        self.closed = True


def _runtime(client: FakeCopilotClient, *, timeout: float = 5.0) -> CopilotRuntime:
    async def factory() -> FakeCopilotClient:
        return client

    return CopilotRuntime(client_factory=factory, timeout=timeout)


def test_status_reports_signed_in_account() -> None:
    rt = _runtime(FakeCopilotClient())
    try:
        state = rt.status()
    finally:
        rt.close()
    assert state.provider_id == "github_copilot"
    assert state.authenticated is True
    assert state.account == "octocat"


def test_status_rejects_token_based_auth_mode() -> None:
    client = FakeCopilotClient(status={"authenticated": True, "authMode": "personal_access_token"})
    rt = _runtime(client)
    try:
        with pytest.raises(AiPolicyBlockedError):
            rt.status()
    finally:
        rt.close()


def test_models_pass_through() -> None:
    rt = _runtime(FakeCopilotClient())
    try:
        assert rt.models() == ["gpt-4o", "o4-mini"]
    finally:
        rt.close()


def test_answer_maps_text_and_model_and_usage_unknown_by_default() -> None:
    rt = _runtime(FakeCopilotClient())
    provider = CopilotProvider(runtime=rt)
    try:
        answer = provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()
    assert answer.text == "Hello from Copilot"
    assert answer.provider == "github_copilot"
    assert answer.model == "gpt-4o"
    # No usage reported by the SDK -> left unknown, never invented.
    assert answer.usage.input_tokens is None
    assert answer.usage.output_tokens is None


def test_answer_reports_usage_when_sdk_provides_it() -> None:
    client = FakeCopilotClient(
        completion={
            "status": "completed",
            "model": "gpt-4o",
            "text": "hi",
            "usage": {"promptTokens": 12, "completionTokens": 3},
        }
    )
    rt = _runtime(client)
    provider = CopilotProvider(runtime=rt)
    try:
        answer = provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()
    assert answer.usage.input_tokens == 12
    assert answer.usage.output_tokens == 3


def test_partial_completion_is_not_an_answer() -> None:
    client = FakeCopilotClient(completion={"status": "in_progress"})
    rt = _runtime(client)
    provider = CopilotProvider(runtime=rt)
    try:
        with pytest.raises(AiInvalidResponseError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_quota_error_is_mapped() -> None:
    client = FakeCopilotClient(raise_on_complete=RuntimeError("Copilot quota exceeded"))
    rt = _runtime(client)
    provider = CopilotProvider(runtime=rt)
    try:
        with pytest.raises(AiQuotaExhaustedError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_expired_login_is_mapped() -> None:
    client = FakeCopilotClient(raise_on_complete=RuntimeError("401 unauthorized; please sign in"))
    rt = _runtime(client)
    provider = CopilotProvider(runtime=rt)
    try:
        with pytest.raises(AiAuthenticationRequiredError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_answer_sends_isolation_and_no_default_model() -> None:
    client = FakeCopilotClient()
    rt = _runtime(client)
    provider = CopilotProvider(runtime=rt, model="")
    try:
        provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()
    assert client.last_complete_kwargs is not None
    assert client.last_complete_kwargs["isolation"]["tools"] is False
    assert client.last_complete_kwargs["isolation"]["mcpServers"] == []
    assert client.last_complete_kwargs["model"] == ""  # no default injected


def test_load_client_without_sdk_is_runtime_unavailable() -> None:
    # The real factory must report a clear, actionable unavailable error
    # when the optional SDK is not installed (which it isn't in CI).
    import asyncio

    with pytest.raises(AiRuntimeUnavailableError):
        asyncio.run(gh._load_client(30.0))


def test_has_conflicting_env_detects_shadowing_tokens() -> None:
    assert gh.has_conflicting_env({"GH_TOKEN": "x"}) == ["GH_TOKEN"]
    assert gh.has_conflicting_env({"PATH": "/usr/bin"}) == []
