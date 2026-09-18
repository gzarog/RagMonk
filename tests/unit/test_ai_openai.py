"""``ai/openai.py`` with a mocked ``httpx`` transport -- zero real network
calls. Covers success, an API error response, and a network failure, all
mapped to ``AiProviderError``.
"""

from __future__ import annotations

import httpx
import pytest

from ragmonk.ai.base import AiProviderError, AiRequest
from ragmonk.ai.openai import OpenAiProvider


def _request() -> AiRequest:
    return AiRequest(question="What does it do?", summary="summary", evidence=[], graph_paths=[])


def _provider(handler: httpx.MockTransport) -> OpenAiProvider:
    return OpenAiProvider(
        api_key="test-key",
        model="gpt-test",
        http_client=httpx.Client(transport=handler),
    )


def test_answer_returns_text_and_usage_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "AnimalService barks."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    answer = provider.answer(_request())

    assert answer.text == "AnimalService barks."
    assert answer.provider == "openai"
    assert answer.model == "gpt-test"
    assert answer.usage.input_tokens == 42
    assert answer.usage.output_tokens == 7


def test_answer_raises_ai_provider_error_on_api_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "invalid api key", "type": "invalid_request_error"}}
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
