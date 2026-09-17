"""Builds the configured ``AiProvider`` from ``ai:``/``privacy:`` config,
applying the ``privacy.external_ai_allowed`` gate (blueprint: a cloud
provider must refuse to run without explicit opt-in) before any provider
object -- let alone any network client -- is even constructed.

Ollama and ``privacy.external_ai_allowed``: this module exempts Ollama
from the flag only when its configured host actually resolves to
loopback (``localhost``/``127.0.0.1``/``::1``), the blueprint's own
local-first framing applied literally -- a request that never leaves the
machine is not "external AI" in the sense the flag exists to gate. A
non-default, remote ``ai.base_url`` (a Ollama server on another host) is
*not* exempt: it is real network egress to a third party, indistinguishable
in kind from OpenAI/Anthropic/an OpenAI-compatible endpoint, so it is
gated identically to them rather than inheriting Ollama's usual pass.
This is a deliberate, narrower reading than "Ollama is always local" --
documented here rather than left as an unstated assumption.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from ragmonk.ai import registry
from ragmonk.ai.base import AiNotConfiguredError, AiPrivacyBlockedError, AiProvider
from ragmonk.core.config import AiConfig, PrivacyConfig

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_local_host(base_url: str) -> bool:
    try:
        hostname = urlparse(base_url).hostname
    except ValueError:
        return False
    return hostname is not None and hostname.lower() in _LOCAL_HOSTS


def _require_external_ai_allowed(privacy: PrivacyConfig, *, label: str) -> None:
    if not privacy.external_ai_allowed:
        raise AiPrivacyBlockedError(
            f"ai.provider={label!r} sends data to a network endpoint outside this machine, "
            "which requires privacy.external_ai_allowed=true (it defaults to false). "
            "Set it with: ragmonk config set privacy.external_ai_allowed true"
        )


def create_provider(
    *, ai: AiConfig, privacy: PrivacyConfig, http_client: httpx.Client | None = None
) -> AiProvider:
    """``http_client`` is a test-only seam: production callers
    (``cli/ask.py``) never pass one, so each provider builds its own real
    HTTP client; tests inject an ``httpx.Client(transport=httpx.MockTransport(...))``
    so this factory's own gating/wiring can be proven with zero real
    network calls, same as each provider module's own unit tests.
    """
    provider = (ai.provider or "none").strip().lower()

    if provider in ("", "none"):
        raise AiNotConfiguredError(
            "no ai.provider is configured. Set one with: "
            "ragmonk config set ai.provider <openai|anthropic|ollama|openai_compatible>"
        )

    # Each branch imports only its own provider module (CLI performance
    # improvement plan, Phase 2): `ai/openai.py` and `ai/anthropic.py` each
    # import their real SDK (`openai`/`anthropic`) at module level, so
    # importing every provider up front here -- regardless of which one is
    # actually configured -- would load both SDKs on every `ragmonk ask`.
    if provider == "openai":
        from ragmonk.ai.openai import DEFAULT_MODEL as _OPENAI_DEFAULT_MODEL
        from ragmonk.ai.openai import OpenAiProvider

        _require_external_ai_allowed(privacy, label="openai")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AiNotConfiguredError("ai.provider=openai requires the OPENAI_API_KEY env var")
        return OpenAiProvider(
            api_key=api_key,
            model=ai.model or _OPENAI_DEFAULT_MODEL,
            base_url=ai.base_url,
            timeout=ai.timeout_seconds,
            http_client=http_client,
        )

    if provider == "anthropic":
        from ragmonk.ai.anthropic import DEFAULT_MODEL as _ANTHROPIC_DEFAULT_MODEL
        from ragmonk.ai.anthropic import AnthropicProvider

        _require_external_ai_allowed(privacy, label="anthropic")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise AiNotConfiguredError(
                "ai.provider=anthropic requires the ANTHROPIC_API_KEY env var"
            )
        return AnthropicProvider(
            api_key=api_key,
            model=ai.model or _ANTHROPIC_DEFAULT_MODEL,
            base_url=ai.base_url,
            timeout=ai.timeout_seconds,
            http_client=http_client,
        )

    if provider == "openai_compatible":
        from ragmonk.ai.openai_compatible import API_KEY_ENV_VAR, OpenAiCompatibleProvider

        _require_external_ai_allowed(privacy, label="openai_compatible")
        if not ai.base_url:
            raise AiNotConfiguredError("ai.provider=openai_compatible requires ai.base_url")
        if not ai.model:
            raise AiNotConfiguredError("ai.provider=openai_compatible requires ai.model")
        return OpenAiCompatibleProvider(
            base_url=ai.base_url,
            model=ai.model,
            api_key=os.environ.get(API_KEY_ENV_VAR, ""),
            timeout=ai.timeout_seconds,
            http_client=http_client,
        )

    if provider == "ollama":
        from ragmonk.ai.ollama import DEFAULT_BASE_URL as _OLLAMA_DEFAULT_BASE_URL
        from ragmonk.ai.ollama import DEFAULT_MODEL as _OLLAMA_DEFAULT_MODEL
        from ragmonk.ai.ollama import OllamaProvider

        base_url = ai.base_url or _OLLAMA_DEFAULT_BASE_URL
        if not _is_local_host(base_url):
            _require_external_ai_allowed(privacy, label="ollama (non-local base_url)")
        return OllamaProvider(
            model=ai.model or _OLLAMA_DEFAULT_MODEL,
            base_url=base_url,
            timeout=ai.timeout_seconds,
            http_client=http_client,
        )

    if provider in registry.SUBSCRIPTION_PROVIDERS:
        return _create_subscription_provider(provider, ai=ai, privacy=privacy)

    raise AiNotConfiguredError(
        f"unknown ai.provider={ai.provider!r}; expected one of "
        "openai, anthropic, ollama, openai_compatible, codex, github_copilot"
    )


def _create_subscription_provider(
    provider: str, *, ai: AiConfig, privacy: PrivacyConfig
) -> AiProvider:
    """Build the ``AiProvider`` for a subscription provider (``codex``,
    ``github_copilot``).

    The privacy gate is applied *first* -- before the adapter module is
    even imported, let alone before it starts any child process -- so
    ``privacy.external_ai_allowed=false`` blocks a cloud subscription
    provider at exactly the same point it blocks every other cloud
    provider (release gate 3). The adapter module is then imported lazily
    (keeping its runtime/SDK off the import path for every other provider)
    and asked for a provider via its ``create_ai_provider`` contract. When
    that module or its runtime is not installed -- the Phase 1 state, where
    no adapter ships yet -- this surfaces a clear ``AiRuntimeUnavailableError``
    rather than a bare ``ImportError``.
    """
    import importlib

    from ragmonk.ai.base import AiRuntimeUnavailableError

    cap = registry.get_capability(provider)
    label = cap.display_name if cap is not None else provider
    _require_external_ai_allowed(privacy, label=provider)

    module_path = registry.RUNTIME_MODULES.get(provider)
    if module_path is None:  # pragma: no cover - guarded by SUBSCRIPTION_PROVIDERS
        raise AiRuntimeUnavailableError(f"no adapter is registered for provider {provider!r}")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise AiRuntimeUnavailableError(
            f"the {label} runtime is not available: {exc}. "
            "Install the required runtime/SDK to use this provider (see docs/providers)."
        ) from exc
    builder = getattr(module, "create_ai_provider", None)
    if builder is None:
        raise AiRuntimeUnavailableError(
            f"the {label} adapter does not provide an answer provider yet"
        )
    return builder(ai=ai, privacy=privacy)
