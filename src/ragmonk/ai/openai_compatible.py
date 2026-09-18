"""A generic OpenAI-compatible chat-completions endpoint (self-hosted or
third-party), reusing the OpenAI SDK itself pointed at a configurable
``base_url`` -- "OpenAI-compatible" means exactly that: the same
``POST {base_url}/chat/completions`` request/response shape ``ai/openai.py``
already speaks, so a second hand-rolled HTTP client would just duplicate
it. What's actually different from ``ai/openai.py`` is the two things
this module owns: ``base_url`` is required (there is no sensible default
for an arbitrary endpoint), and the API key comes from a different,
generic environment variable rather than ``OPENAI_API_KEY``, since this
is deliberately not OpenAI's own service.
"""

from __future__ import annotations

import httpx
import openai as openai_sdk

from ragmonk.ai.base import AiAnswer, AiProviderError, AiRequest, AiUsage, build_messages

API_KEY_ENV_VAR = "RAGMONK_AI_API_KEY"


class OpenAiCompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        # Some self-hosted endpoints accept any placeholder credential
        # (or none at all); the SDK itself requires a non-empty string,
        # so an unset key becomes a harmless placeholder rather than a
        # hard failure at construction time -- the endpoint, not
        # RagMonk, decides whether that placeholder is accepted.
        # max_retries=0: see ai/openai.py's identical comment.
        self._client = openai_sdk.OpenAI(
            api_key=api_key or "not-required",
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def answer(self, request: AiRequest) -> AiAnswer:
        try:
            response = self._client.chat.completions.create(
                model=self._model, messages=build_messages(request)  # type: ignore[arg-type]
            )
        except openai_sdk.OpenAIError as exc:
            raise AiProviderError(f"openai-compatible request failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice is not None else ""
        usage = response.usage
        return AiAnswer(
            text=text,
            provider="openai_compatible",
            model=response.model or self._model,
            usage=AiUsage(
                input_tokens=usage.prompt_tokens if usage is not None else None,
                output_tokens=usage.completion_tokens if usage is not None else None,
            ),
        )
