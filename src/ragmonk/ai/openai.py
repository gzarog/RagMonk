"""Real OpenAI Chat Completions client (the ``openai`` PyPI package).

``pyproject.toml`` pins ``openai>=1.0,<2``: the well-documented, stable
v1.x ``OpenAI(...).chat.completions.create(...)`` surface, built on plain
``httpx`` -- confirmed against the actual installed package rather than
assumed, since this environment's PyPI index also serves a much newer
major (3.x) that vendors its own forked HTTP client internally and would
undermine the "inject a fake ``httpx.Client``" testing approach below.
"""

from __future__ import annotations

import httpx
import openai as openai_sdk

from ragmonk.ai.base import AiAnswer, AiProviderError, AiRequest, AiUsage, build_messages

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAiProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        # ``http_client`` is the test seam (see tests/unit/test_ai_openai.py):
        # a real caller leaves it unset and the SDK builds its own;
        # a test passes ``httpx.Client(transport=httpx.MockTransport(...))``
        # so not one byte of this ever reaches a real network socket.
        # max_retries=0: RagMonk surfaces a provider failure immediately
        # as ``AiProviderError`` rather than layering the SDK's own
        # exponential-backoff retries underneath a CLI command a human is
        # waiting on -- also what keeps a mocked network-failure test
        # fast and deterministic instead of sleeping through retry delays.
        self._client = openai_sdk.OpenAI(
            api_key=api_key,
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
            raise AiProviderError(f"openai request failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice is not None else ""
        usage = response.usage
        return AiAnswer(
            text=text,
            provider="openai",
            model=response.model or self._model,
            usage=AiUsage(
                input_tokens=usage.prompt_tokens if usage is not None else None,
                output_tokens=usage.completion_tokens if usage is not None else None,
            ),
        )
