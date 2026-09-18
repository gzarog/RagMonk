"""Codex adapter contract tests (subscription plan, Phase 2).

The whole adapter stack -- ``CodexRuntime`` -> ``RuntimeBridge`` ->
``CodexSession`` -> ``JsonRpcClient`` -- runs against a scripted in-memory
byte stream, so there is no ChatGPT account, no ``codex`` binary, and no
network. Only the raw stdio bytes are faked; every mapping decision
(auth-mode rejection, quota/expired-login translation, partial-as-final
refusal, model/usage extraction) is the real code under test.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ragmonk.ai import codex
from ragmonk.ai._transport import JsonRpcClient
from ragmonk.ai.base import (
    AiAuthenticationRequiredError,
    AiInvalidResponseError,
    AiPolicyBlockedError,
    AiQuotaExhaustedError,
    AiRequest,
    AiTimeoutError,
)
from ragmonk.ai.codex import CodexProvider, CodexRuntime


class CodexScript:
    """A scripted Codex App Server. Each write is answered from
    ``responses``/``errors``; methods in ``silent`` are never answered
    (timeout); methods in ``malformed`` return junk.
    """

    def __init__(self) -> None:
        self.responses: dict[str, dict] = {
            "initialize": {"serverInfo": {"name": "codex"}},
            "account/getStatus": {"authenticated": True, "account": {"email": "u@example.com"}},
            "account/login": {"authenticated": True, "account": {"email": "u@example.com"}},
            "account/logout": {},
            "model/list": {"models": [{"id": "gpt-x"}, {"name": "gpt-y"}, "gpt-z"]},
            "thread/create": {"threadId": "t1"},
            "thread/runTurn": {
                "status": "completed",
                "model": "gpt-x",
                "message": {"content": [{"type": "text", "text": "Hello from Codex"}]},
                "usage": {"inputTokens": 11, "outputTokens": 7},
            },
        }
        self.errors: dict[str, tuple[object, str]] = {}
        self.silent: set[str] = set()
        self.malformed: set[str] = set()

    def __call__(self, message: dict) -> list[bytes]:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            return []  # notification (e.g. "initialized")
        if method in self.silent:
            return []
        if method in self.malformed:
            return [b"{not-json\n"]
        if method in self.errors:
            code, text = self.errors[method]
            return [
                json.dumps(
                    {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}
                ).encode()
                + b"\n"
            ]
        result = self.responses.get(method, {})
        return [json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode() + b"\n"]


def _runtime(script: CodexScript, *, timeout: float = 5.0) -> CodexRuntime:
    async def factory() -> codex.CodexSession:
        client = JsonRpcClient(_Stream(script))
        await client.start()
        return codex.CodexSession(client, timeout=timeout)

    return CodexRuntime(session_factory=factory, timeout=timeout)


class _Stream:
    def __init__(self, responder: CodexScript) -> None:
        self._responder = responder
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self) -> bytes:
        return await self._queue.get()

    async def write(self, data: bytes) -> None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            message = {}
        for chunk in self._responder(message):
            self._queue.put_nowait(chunk)

    async def aclose(self) -> None:
        return None


# --- version helpers (pure, no runtime) ---------------------------------


def test_parse_version_extracts_semver() -> None:
    assert codex._parse_version("codex 1.4.2 (build)") == (1, 4, 2)
    assert codex._parse_version("v2.0") == (2, 0, 0)
    assert codex._parse_version("no version here") is None


def test_check_supported_version_rejects_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "_MIN_TESTED_VERSION", (1, 0, 0))
    from ragmonk.ai.base import AiUnsupportedVersionError

    with pytest.raises(AiUnsupportedVersionError):
        codex._check_supported_version((0, 9, 0))
    codex._check_supported_version((1, 2, 0))  # supported -> no raise
    codex._check_supported_version(None)  # unknown -> no raise


# --- lifecycle ----------------------------------------------------------


def test_status_reports_authenticated_account() -> None:
    rt = _runtime(CodexScript())
    try:
        state = rt.status()
    finally:
        rt.close()
    assert state.provider_id == "codex"
    assert state.authenticated is True
    assert state.account == "u@example.com"


def test_status_rejects_unexpected_auth_mode() -> None:
    script = CodexScript()
    script.responses["account/getStatus"] = {"authenticated": True, "authMode": "api_key"}
    rt = _runtime(script)
    try:
        with pytest.raises(AiPolicyBlockedError):
            rt.status()
    finally:
        rt.close()


def test_models_are_parsed_from_mixed_shapes() -> None:
    rt = _runtime(CodexScript())
    try:
        assert rt.models() == ["gpt-x", "gpt-y", "gpt-z"]
    finally:
        rt.close()


def test_login_and_logout_round_trip() -> None:
    rt = _runtime(CodexScript())
    try:
        assert rt.login().authenticated is True
        rt.logout()  # no raise
    finally:
        rt.close()


# --- answer path --------------------------------------------------------


def test_answer_maps_text_model_and_usage() -> None:
    rt = _runtime(CodexScript())
    provider = CodexProvider(runtime=rt)
    try:
        answer = provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()
    assert answer.text == "Hello from Codex"
    assert answer.provider == "codex"
    assert answer.model == "gpt-x"
    assert answer.usage.input_tokens == 11
    assert answer.usage.output_tokens == 7


def test_partial_turn_is_not_presented_as_an_answer() -> None:
    script = CodexScript()
    script.responses["thread/runTurn"] = {"status": "in_progress"}
    rt = _runtime(script)
    provider = CodexProvider(runtime=rt)
    try:
        with pytest.raises(AiInvalidResponseError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_empty_answer_text_is_invalid_response() -> None:
    script = CodexScript()
    script.responses["thread/runTurn"] = {"status": "completed", "message": {"content": []}}
    rt = _runtime(script)
    provider = CodexProvider(runtime=rt)
    try:
        with pytest.raises(AiInvalidResponseError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_quota_error_is_mapped() -> None:
    script = CodexScript()
    script.errors["thread/runTurn"] = ("quota_exceeded", "monthly limit reached")
    rt = _runtime(script)
    provider = CodexProvider(runtime=rt)
    try:
        with pytest.raises(AiQuotaExhaustedError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_expired_login_is_mapped_to_authentication_required() -> None:
    script = CodexScript()
    script.errors["thread/runTurn"] = ("unauthenticated", "token expired")
    rt = _runtime(script)
    provider = CodexProvider(runtime=rt)
    try:
        with pytest.raises(AiAuthenticationRequiredError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_runtime_timeout_is_mapped() -> None:
    script = CodexScript()
    script.silent.add("thread/runTurn")
    rt = _runtime(script, timeout=0.3)
    provider = CodexProvider(runtime=rt)
    try:
        with pytest.raises(AiTimeoutError):
            provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()


def test_answer_sends_isolation_and_no_openai_default_model() -> None:
    """The runTurn request must carry the isolation config and, with no
    configured model, must not smuggle in the OpenAI API adapter's default.
    """
    seen: dict[str, dict] = {}

    class RecordingScript(CodexScript):
        def __call__(self, message: dict) -> list[bytes]:
            if message.get("method") == "thread/runTurn":
                seen["runTurn"] = message.get("params", {})
            return super().__call__(message)

    rt = _runtime(RecordingScript())
    provider = CodexProvider(runtime=rt, model="")
    try:
        provider.answer(AiRequest(question="Q", summary="S"))
    finally:
        rt.close()
    params = seen["runTurn"]
    assert params["isolation"]["tools"] is False
    assert params["isolation"]["mcpServers"] == []
    assert "model" not in params  # no default model injected
