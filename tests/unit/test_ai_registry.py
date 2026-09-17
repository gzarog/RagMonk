"""The provider capability registry (subscription plan, Phase 1): purely
declarative facts, so every assertion here is about the table itself --
importing it must pull in no SDK or runtime.
"""

from __future__ import annotations

from ragmonk.ai import registry


def test_all_capabilities_is_name_sorted_and_complete() -> None:
    caps = registry.all_capabilities()
    ids = [cap.provider_id for cap in caps]
    assert ids == sorted(ids)
    assert set(ids) == set(registry.REGISTRY)


def test_known_providers_includes_none_sentinel_and_every_registered_id() -> None:
    assert "none" in registry.KNOWN_PROVIDERS
    for name in registry.REGISTRY:
        assert name in registry.KNOWN_PROVIDERS


def test_subscription_providers_are_exactly_codex_and_github_copilot() -> None:
    assert set(registry.SUBSCRIPTION_PROVIDERS) == {"codex", "github_copilot"}
    assert registry.is_subscription_provider("codex")
    assert registry.is_subscription_provider("GITHUB_COPILOT")  # case-insensitive
    assert not registry.is_subscription_provider("openai")


def test_subscription_providers_store_no_api_key_env() -> None:
    for name in registry.SUBSCRIPTION_PROVIDERS:
        assert registry.REGISTRY[name].api_key_env is None


def test_ollama_is_the_only_non_cloud_provider() -> None:
    non_cloud = [c.provider_id for c in registry.all_capabilities() if not c.cloud_egress]
    assert non_cloud == ["ollama"]
    assert not registry.requires_external_ai("ollama")
    assert registry.requires_external_ai("codex")


def test_every_subscription_provider_has_a_runtime_module_mapping() -> None:
    for name in registry.SUBSCRIPTION_PROVIDERS:
        assert name in registry.RUNTIME_MODULES


def test_get_capability_unknown_returns_none() -> None:
    assert registry.get_capability("not-a-provider") is None
