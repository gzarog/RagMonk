"""Provider capability registry (subscription plan, Phase 1).

One declarative table describing every ``ai.provider`` RagMonk knows
about: how it authenticates, whether it sends data off the machine,
whether it is a *subscription* provider (account sign-in rather than an
API key), how models are discovered, and how usage is reported. This is
deliberately data, not behavior -- ``ai/factory.py`` still owns *building*
a provider and ``cli/ai.py`` owns the lifecycle commands; both read this
table so "what kind of provider is this?" has exactly one answer instead
of a growing pile of ``if provider == ...`` checks scattered across
modules.

Importing this module is cheap: it pulls in no SDK, no runtime, and no
network client, so ``ragmonk ai providers`` (and ``--help``) can list and
describe every provider without paying for any of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapability:
    """Static facts about one provider. Nothing here reaches out to a
    runtime -- a field like ``model_discovery="dynamic"`` states the
    *contract* (the adapter resolves models from its runtime at call
    time), not a live query result.
    """

    provider_id: str
    display_name: str
    # How a caller authenticates. ``auth_modes`` is the closed set the
    # adapter accepts; ``default_auth_mode`` is what config defaults to.
    auth_modes: tuple[str, ...]
    default_auth_mode: str
    # True if a request leaves this machine (gated by
    # ``privacy.external_ai_allowed``); False only for loopback Ollama.
    cloud_egress: bool
    # True for account-sign-in providers (codex, github_copilot) as
    # opposed to API-key or local providers.
    subscription: bool
    # The env var an API-key provider reads, or None for subscription/
    # local providers that never read one.
    api_key_env: str | None
    # "dynamic": models come from the runtime at call time; "static": a
    # fixed default model name; "config": the user must set ai.model.
    model_discovery: str
    # "tokens": input/output token counts reported; "unknown": the
    # runtime may not report usage; "none": no usage available.
    usage_reporting: str
    # A subscription adapter still hardening behind its release gates.
    beta: bool
    notes: str


_LOCAL_NOTE = "Local inference; loopback host is exempt from the privacy flag."

REGISTRY: dict[str, ProviderCapability] = {
    "openai": ProviderCapability(
        provider_id="openai",
        display_name="OpenAI API",
        auth_modes=("api_key",),
        default_auth_mode="api_key",
        cloud_egress=True,
        subscription=False,
        api_key_env="OPENAI_API_KEY",
        model_discovery="static",
        usage_reporting="tokens",
        beta=False,
        notes="API-key provider; reads OPENAI_API_KEY from the environment.",
    ),
    "anthropic": ProviderCapability(
        provider_id="anthropic",
        display_name="Anthropic API",
        auth_modes=("api_key",),
        default_auth_mode="api_key",
        cloud_egress=True,
        subscription=False,
        api_key_env="ANTHROPIC_API_KEY",
        model_discovery="static",
        usage_reporting="tokens",
        beta=False,
        notes="API-key provider; reads ANTHROPIC_API_KEY from the environment.",
    ),
    "openai_compatible": ProviderCapability(
        provider_id="openai_compatible",
        display_name="OpenAI-compatible endpoint",
        auth_modes=("api_key",),
        default_auth_mode="api_key",
        cloud_egress=True,
        subscription=False,
        api_key_env="RAGMONK_OPENAI_COMPATIBLE_API_KEY",
        model_discovery="config",
        usage_reporting="unknown",
        beta=False,
        notes="Self-hosted or third-party endpoint; requires ai.base_url and ai.model.",
    ),
    "ollama": ProviderCapability(
        provider_id="ollama",
        display_name="Ollama (local)",
        auth_modes=("none",),
        default_auth_mode="none",
        cloud_egress=False,
        subscription=False,
        api_key_env=None,
        model_discovery="config",
        usage_reporting="none",
        beta=False,
        notes=_LOCAL_NOTE,
    ),
    "codex": ProviderCapability(
        provider_id="codex",
        display_name="ChatGPT via Codex (subscription)",
        auth_modes=("chatgpt",),
        default_auth_mode="chatgpt",
        cloud_egress=True,
        subscription=True,
        api_key_env=None,
        model_discovery="dynamic",
        usage_reporting="unknown",
        beta=True,
        notes=(
            "Uses a signed-in ChatGPT account through the official Codex runtime; "
            "consumes the account allowance. Beta until its version/isolation gates pass. "
            "Never reuses OPENAI_API_KEY."
        ),
    ),
    "github_copilot": ProviderCapability(
        provider_id="github_copilot",
        display_name="GitHub Copilot (subscription)",
        auth_modes=("signed_in_user",),
        default_auth_mode="signed_in_user",
        cloud_egress=True,
        subscription=True,
        api_key_env=None,
        model_discovery="dynamic",
        usage_reporting="unknown",
        beta=True,
        notes=(
            "Uses the signed-in GitHub Copilot CLI credentials through the official SDK; "
            "consumes the Copilot allowance. Never uses GH_TOKEN/GITHUB_TOKEN or a provider key."
        ),
    ),
}

# Every provider name ``ai.provider`` may be set to, plus the ``"none"``
# sentinel meaning "not configured". Kept in sync with ``REGISTRY`` so
# there is one source of truth for validation (``core/config.py``) and
# for ``ai/factory.py``'s dispatch.
KNOWN_PROVIDERS: frozenset[str] = frozenset(REGISTRY) | {"none"}

# Subscription providers whose answer adapter/runtime is delivered in a
# later phase. In Phase 1 no concrete runtime is registered, so their
# lifecycle/answer paths report ``runtime_unavailable`` cleanly.
SUBSCRIPTION_PROVIDERS: tuple[str, ...] = tuple(
    cap.provider_id for cap in REGISTRY.values() if cap.subscription
)

# Subscription provider id -> the module expected to expose both
# ``create_runtime(ai)`` (lifecycle, for ``cli/ai.py``) and
# ``create_ai_provider(ai, privacy)`` (the ``AiProvider`` for
# ``ragmonk ask``). The modules are added by later phases; naming them
# here, in the one place that already owns provider facts, lets both
# ``ai/factory.py`` and ``ai/runtime.py`` find them without importing
# them and without a second copy of the map.
RUNTIME_MODULES: dict[str, str] = {
    "codex": "ragmonk.ai.codex",
    "github_copilot": "ragmonk.ai.github_copilot",
}


def get_capability(provider_id: str) -> ProviderCapability | None:
    return REGISTRY.get(provider_id.strip().lower())


def all_capabilities() -> list[ProviderCapability]:
    """Stable, name-sorted order so ``ragmonk ai providers`` output does
    not shuffle between runs.
    """
    return [REGISTRY[name] for name in sorted(REGISTRY)]


def is_subscription_provider(provider_id: str) -> bool:
    cap = get_capability(provider_id)
    return cap is not None and cap.subscription


def requires_external_ai(provider_id: str) -> bool:
    """Whether selecting this provider crosses the
    ``privacy.external_ai_allowed`` gate. True for every cloud provider;
    Ollama's loopback exemption is decided by ``ai/factory.py`` against
    the *actual* configured host, so this only reflects the provider's
    default egress posture.
    """
    cap = get_capability(provider_id)
    return cap is not None and cap.cloud_egress
