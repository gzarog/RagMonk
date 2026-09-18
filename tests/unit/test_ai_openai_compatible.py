"""``ai/openai_compatible.py`` with a mocked ``httpx`` transport -- zero
real network calls.
"""

from __future__ import annotations

import httpx
import pytest

from ragmonk.ai.base import AiProviderError, AiRequest
from ragmonk.ai.openai_compatible import OpenAiCompatibleProvider


def _request() -> AiRequest:
    return AiRequest(question="What does it do?", summary="summary", evidence=[], graph_paths=[])


def _provider(handler: httpx.MockTransport) -> OpenAiCompatibleProvider:
    return OpenAiCompatibleProvider(
        base_url="https://self-hosted.example.com/v1",
        model="local-model",
        http_client=httpx.Client(transport=handler),
    )


def test_answer_returns_text_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://self-hosted.example.com/v1")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "self-hosted answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    answer = provider.answer(_request())

    assert answer.text == "self-hosted answer"
    assert answer.provider == "openai_compatible"


def test_answer_raises_ai_provider_error_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(AiProviderError):
        provider.answer(_request())
