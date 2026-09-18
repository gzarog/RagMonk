"""AI provider administration for the admin UI.

Admin UI plan, Phase 7 (§10): show the selected provider and its
connection state, list known providers, and test connectivity -- without
ever revealing credentials. Reuses ``ai.registry`` and, for subscription
providers, their own official-runtime ``status()`` (which is defined to
carry no secret).
"""

from __future__ import annotations

import os
from typing import Any

from ragmonk.ai import registry
from ragmonk.core.lifecycle import AppContext


def provider_overview(ctx: AppContext) -> dict[str, Any]:
    """Current selection plus the full provider catalog (§10.1)."""
    ai = ctx.config.ai
    caps = registry.all_capabilities()
    selected = ai.provider
    cap = registry.get_capability(selected)
    return {
        "selected": selected,
        "model": ai.model or None,
        "base_url": ai.base_url,
        "external_ai_allowed": ctx.config.privacy.external_ai_allowed,
        "auth_status": _auth_status(selected, cap),
        "providers": [
            {
                "provider_id": c.provider_id,
                "display_name": c.display_name,
                "subscription": c.subscription,
                "cloud_egress": c.cloud_egress,
                "beta": c.beta,
                "api_key_env": c.api_key_env,
                "model_discovery": c.model_discovery,
                "selected": c.provider_id == selected,
            }
            for c in caps
        ],
    }


def _auth_status(provider_id: str, cap: registry.ProviderCapability | None) -> str:
    if provider_id == "none" or cap is None:
        return "not configured"
    if cap.api_key_env:
        return "configured" if os.environ.get(cap.api_key_env) else "not configured"
    if cap.subscription:
        return "account-based (test to check sign-in)"
    return "local/no auth"


def test_provider(ctx: AppContext, provider_id: str) -> dict[str, Any]:
    """Test a provider's reachability/auth without revealing secrets (§10.2).

    * subscription providers: ask the official runtime's ``status()``.
    * api-key providers: report whether the key env var is set.
    * local/openai-compatible: report the configured base URL (a live
      probe is deliberately avoided here to keep the test dependency-free
      and non-blocking; reachability is surfaced by ``ragmonk doctor``).
    """
    cap = registry.get_capability(provider_id)
    if cap is None:
        return {"provider": provider_id, "ok": False, "detail": "unknown provider"}

    if cap.subscription:
        try:
            from ragmonk.ai.runtime import resolve_runtime  # noqa: PLC0415

            runtime = resolve_runtime(cap.provider_id, ai=ctx.config.ai)
            try:
                status = runtime.status()
            finally:
                runtime.close()
            return {
                "provider": provider_id,
                "ok": status.authenticated,
                "detail": status.detail,
                "account": status.account,
            }
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a failed test
            return {"provider": provider_id, "ok": False, "detail": str(exc)}

    if cap.api_key_env:
        present = bool(os.environ.get(cap.api_key_env))
        return {
            "provider": provider_id,
            "ok": present,
            "detail": (
                f"{cap.api_key_env} is set" if present else f"{cap.api_key_env} is not set"
            ),
        }

    base_url = ctx.config.ai.base_url or "(default)"
    return {
        "provider": provider_id,
        "ok": True,
        "detail": f"local provider; base URL {base_url}",
    }
