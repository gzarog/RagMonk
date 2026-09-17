"""Real Ollama client -- a local HTTP API, no dedicated SDK package
exists for it, so this talks to ``/api/chat`` directly via ``httpx``
(already a direct dependency; see ``pyproject.toml``).

Default ``base_url`` is ``http://localhost:11434``, Ollama's own default
bind address -- per ``ai/factory.py``'s privacy-gating decision, this
provider is exempt from ``privacy.external_ai_allowed`` only when its
configured host actually resolves to loopback; a remote Ollama server is
gated exactly like any other network provider.
"""

from __future__ import annotations

from typing import Any

import httpx

from ragmonk.ai.base import AiAnswer, AiProviderError, AiRequest, AiUsage, build_messages

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"


class OllamaProvider:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        # Same DI seam as the OpenAI/Anthropic providers: a real caller
        # leaves ``http_client`` unset (a plain ``httpx.Client`` is
        # built), a test injects one wrapping ``httpx.MockTransport``.
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self._timeout = timeout

    def answer(self, request: AiRequest) -> AiAnswer:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": build_messages(request),
            "stream": False,
        }
        try:
            response = self._client.post(
                f"{self._base_url}/api/chat", json=payload, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise AiProviderError(f"ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AiProviderError(
                f"ollama returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AiProviderError(f"ollama returned a non-JSON response: {exc}") from exc

        text = data.get("message", {}).get("content", "")
        return AiAnswer(
            text=text,
            provider="ollama",
            model=data.get("model") or self._model,
            usage=AiUsage(
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
            ),
        )
