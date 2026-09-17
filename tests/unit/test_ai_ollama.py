"""``ai/ollama.py`` with a mocked ``httpx`` transport -- zero real
network/local server calls. Covers success, an HTTP error response, and a
network failure, all mapped to ``AiProviderError``.
"""

from __future__ import annotations

import httpx
import pytest

from ragmonk.ai.base import AiProviderError, AiRequest
from ragmonk.ai.ollama import OllamaProvider


def _request() -> AiRequest:
    return AiRequest(question="What does it do?", summary="summary", evidence=[], graph_paths=[])


def _provider(handler: httpx.MockTransport) -> OllamaProvider:
    return OllamaProvider(model="llama-test", http_client=httpx.Client(transport=handler))


def test_answer_returns_text_and_usage_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "model": "llama-test",
                "message": {"role": "assistant", "content": "AnimalService barks."},
                "done": True,
                "prompt_eval_count": 20,
                "eval_count": 4,
            },
        )

    provider = _provider(httpx.MockTransport(handler))
    answer = provider.answer(_request())

    assert answer.text == "AnimalService barks."
    assert answer.provider == "ollama"
    assert answer.model == "llama-test"
    assert answer.usage.input_tokens == 20
    assert answer.usage.output_tokens == 4


def test_answer_raises_ai_provider_error_on_http_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(AiProviderError):
        provider.answer(_request())


def test_answer_raises_ai_provider_error_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(AiProviderError):
        provider.answer(_request())
