"""``ai/anthropic.py`` with a mocked ``httpx`` transport -- zero real
network calls. Covers success, an API error response, and a network
failure, all mapped to ``AiProviderError``.
"""

from __future__ import annotations

import httpx
import pytest

from ragmonk.ai.anthropic import AnthropicProvider
from ragmonk.ai.base import AiProviderError, AiRequest


def _request() -> AiRequest:
    return AiRequest(question="What does it do?", summary="summary", evidence=[], graph_paths=[])


def _provider(handler: httpx.MockTransport) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="test-key",
        model="claude-test",
        http_client=httpx.Client(transport=handler),
    )


def test_answer_returns_text_and_usage_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "AnimalService barks."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 30, "output_tokens": 5},
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    answer = provider.answer(_request())

    assert answer.text == "AnimalService barks."
    assert answer.provider == "anthropic"
    assert answer.model == "claude-test"
    assert answer.usage.input_tokens == 30
    assert answer.usage.output_tokens == 5


def test_answer_raises_ai_provider_error_on_api_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        )

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(AiProviderError):
        provider.answer(_request())


def test_answer_raises_ai_provider_error_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(AiProviderError):
        provider.answer(_request())
