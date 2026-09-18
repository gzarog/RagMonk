"""Real Anthropic Messages API client (the ``anthropic`` PyPI package).

Pinned the same way as ``ai/openai.py``: ``anthropic>=0.25,<1`` for the
well-documented, ``httpx``-based ``Anthropic(...).messages.create(...)``
surface, not this index's much newer (and internally forked-HTTP-client)
1.x/3.x-style majors.
"""

from __future__ import annotations

import anthropic as anthropic_sdk
import httpx

from ragmonk.ai.base import AiAnswer, AiProviderError, AiRequest, AiUsage, build_messages

DEFAULT_MODEL = "claude-3-5-haiku-latest"
DEFAULT_MAX_TOKENS = 1024


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        # max_retries=0: see ai/openai.py's identical comment.
        self._client = anthropic_sdk.Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def answer(self, request: AiRequest) -> AiAnswer:
        messages = build_messages(request)
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        user_messages = [m for m in messages if m["role"] != "system"]
        # ``system`` is always populated by ``build_messages`` in
        # practice, but the SDK's ``create`` rejects a bare ``None`` for
        # it (it wants its own ``omit`` sentinel when omitted) --
        # falling back to that sentinel keeps this correct even if a
        # future ``build_messages`` ever produced a system-message-free
        # request.
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system if system is not None else anthropic_sdk.omit,
                messages=user_messages,  # type: ignore[arg-type]
            )
        except anthropic_sdk.AnthropicError as exc:
            raise AiProviderError(f"anthropic request failed: {exc}") from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return AiAnswer(
            text=text,
            provider="anthropic",
            model=response.model or self._model,
            usage=AiUsage(
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
            ),
        )
