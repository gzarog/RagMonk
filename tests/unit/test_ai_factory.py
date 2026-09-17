"""``ai/factory.py``'s gating logic: no real provider is ever
constructed with real credentials or a real network client here -- every
assertion is about which typed error is raised (or which provider class
comes back) before any HTTP call would happen.
"""

from __future__ import annotations

import pytest

from ragmonk.ai.anthropic import AnthropicProvider
from ragmonk.ai.base import (
    AiNotConfiguredError,
    AiPrivacyBlockedError,
    AiRuntimeUnavailableError,
)
from ragmonk.ai.factory import create_provider
from ragmonk.ai.ollama import OllamaProvider
from ragmonk.ai.openai import OpenAiProvider
from ragmonk.ai.openai_compatible import OpenAiCompatibleProvider
from ragmonk.core.config import AiConfig, PrivacyConfig


def test_no_provider_configured_raises_not_configured() -> None:
    with pytest.raises(AiNotConfiguredError):
        create_provider(ai=AiConfig(), privacy=PrivacyConfig())


def test_unknown_provider_name_raises_not_configured() -> None:
    with pytest.raises(AiNotConfiguredError):
        create_provider(ai=AiConfig(provider="not-a-real-provider"), privacy=PrivacyConfig())


def test_openai_without_privacy_flag_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(provider="openai", model="gpt-test"),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_openai_without_api_key_env_var_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AiNotConfiguredError):
        create_provider(
            ai=AiConfig(provider="openai", model="gpt-test"),
            privacy=PrivacyConfig(external_ai_allowed=True),
        )


def test_openai_with_key_and_privacy_allowed_builds_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = create_provider(
        ai=AiConfig(provider="openai", model="gpt-test"),
        privacy=PrivacyConfig(external_ai_allowed=True),
    )
    assert isinstance(provider, OpenAiProvider)


def test_anthropic_without_privacy_flag_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(provider="anthropic", model="claude-test"),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_anthropic_with_key_and_privacy_allowed_builds_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    provider = create_provider(
        ai=AiConfig(provider="anthropic", model="claude-test"),
        privacy=PrivacyConfig(external_ai_allowed=True),
    )
    assert isinstance(provider, AnthropicProvider)


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(AiNotConfiguredError):
        create_provider(
            ai=AiConfig(provider="openai_compatible", model="local-model"),
            privacy=PrivacyConfig(external_ai_allowed=True),
        )


def test_openai_compatible_without_privacy_flag_is_blocked() -> None:
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(
                provider="openai_compatible",
                model="local-model",
                base_url="https://self-hosted.example.com/v1",
            ),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_openai_compatible_with_base_url_and_privacy_allowed_builds_a_provider() -> None:
    provider = create_provider(
        ai=AiConfig(
            provider="openai_compatible",
            model="local-model",
            base_url="https://self-hosted.example.com/v1",
        ),
        privacy=PrivacyConfig(external_ai_allowed=True),
    )
    assert isinstance(provider, OpenAiCompatibleProvider)


def test_ollama_default_localhost_is_exempt_from_privacy_flag() -> None:
    """The blueprint's local-first exemption: a default (loopback)
    Ollama endpoint never leaves the machine, so it needs no explicit
    ``privacy.external_ai_allowed`` opt-in.
    """
    provider = create_provider(
        ai=AiConfig(provider="ollama", model="llama-test"),
        privacy=PrivacyConfig(external_ai_allowed=False),
    )
    assert isinstance(provider, OllamaProvider)


def test_ollama_explicit_localhost_base_url_is_exempt_from_privacy_flag() -> None:
    provider = create_provider(
        ai=AiConfig(provider="ollama", model="llama-test", base_url="http://127.0.0.1:11434"),
        privacy=PrivacyConfig(external_ai_allowed=False),
    )
    assert isinstance(provider, OllamaProvider)


def test_ollama_remote_base_url_is_not_exempt_from_privacy_flag() -> None:
    """A non-loopback ``base_url`` is real network egress to a third
    party, no different in kind from OpenAI/Anthropic -- it is gated
    identically, not covered by Ollama's usual local-first pass.
    """
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(
                provider="ollama", model="llama-test", base_url="http://ollama.example.com:11434"
            ),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_ollama_remote_base_url_with_privacy_allowed_builds_a_provider() -> None:
    provider = create_provider(
        ai=AiConfig(
            provider="ollama", model="llama-test", base_url="http://ollama.example.com:11434"
        ),
        privacy=PrivacyConfig(external_ai_allowed=True),
    )
    assert isinstance(provider, OllamaProvider)


def test_codex_without_privacy_flag_is_blocked_before_runtime_lookup() -> None:
    """A subscription provider must hit the privacy gate first -- the block
    is raised before its (absent, in Phase 1) adapter is even imported.
    """
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(provider="codex"),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_codex_with_privacy_allowed_reports_runtime_unavailable_in_phase_1() -> None:
    with pytest.raises(AiRuntimeUnavailableError):
        create_provider(
            ai=AiConfig(provider="codex"),
            privacy=PrivacyConfig(external_ai_allowed=True),
        )


def test_github_copilot_without_privacy_flag_is_blocked() -> None:
    with pytest.raises(AiPrivacyBlockedError):
        create_provider(
            ai=AiConfig(provider="github_copilot"),
            privacy=PrivacyConfig(external_ai_allowed=False),
        )


def test_github_copilot_with_privacy_allowed_reports_runtime_unavailable_in_phase_1() -> None:
    with pytest.raises(AiRuntimeUnavailableError):
        create_provider(
            ai=AiConfig(provider="github_copilot"),
            privacy=PrivacyConfig(external_ai_allowed=True),
        )
