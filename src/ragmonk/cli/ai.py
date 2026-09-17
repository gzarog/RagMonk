"""``ragmonk ai providers|status|login|logout|models`` -- subscription
provider lifecycle and diagnostics (subscription plan, Phase 1).

Everything expensive is imported lazily *inside* a command body: the only
module-level imports are Typer, the light capability registry, the typed
errors, and the config loader. So ``ragmonk ai --help`` (and the root
``--help``/``version``) never starts a runtime or loads a provider SDK --
a Phase 1 exit requirement.

Credentials are never accepted, printed, or stored by these commands: sign
in and sign out are delegated to each provider's own official runtime, and
``status`` prints only a non-secret connection snapshot.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ragmonk.ai import registry
from ragmonk.ai.base import AiPolicyBlockedError
from ragmonk.core.config import load_config
from ragmonk.core.errors import UsageError
from ragmonk.core.paths import ensure_runtime_layout

from ._common import cli_command, console, print_json

app = typer.Typer(no_args_is_help=True, help="Manage subscription AI providers.")


def _require_subscription(provider_id: str) -> registry.ProviderCapability:
    cap = registry.get_capability(provider_id)
    if cap is None:
        raise UsageError(
            f"unknown ai provider {provider_id!r}; run 'ragmonk ai providers' to list them"
        )
    if not cap.subscription:
        raise UsageError(
            f"provider {provider_id!r} does not use account sign-in "
            "(it is an API-key or local provider); this command applies only to "
            f"subscription providers: {', '.join(registry.SUBSCRIPTION_PROVIDERS)}"
        )
    return cap


def _gate_privacy(external_ai_allowed: bool, cap: registry.ProviderCapability) -> None:
    """Refuse to contact a cloud runtime while the privacy flag is off --
    applied here, before ``resolve_runtime`` can start any child process,
    mirroring ``ai/factory.py``'s own pre-construction gate.
    """
    if cap.cloud_egress and not external_ai_allowed:
        raise AiPolicyBlockedError(
            f"ai provider {cap.provider_id!r} contacts a cloud runtime, which requires "
            "privacy.external_ai_allowed=true (it defaults to false). "
            "Set it with: ragmonk config set privacy.external_ai_allowed true"
        )


@app.command("providers")
@cli_command
def providers(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List every AI provider RagMonk knows about and its capabilities."""
    caps = registry.all_capabilities()
    if json_output:
        print_json({"providers": [vars(cap) for cap in caps]})
        return
    for cap in caps:
        kind = "subscription" if cap.subscription else ("local" if not cap.cloud_egress else "api")
        beta = " [beta]" if cap.beta else ""
        console.print(f"[bold cyan]{cap.provider_id}[/bold cyan]{beta} — {cap.display_name}")
        console.print(
            f"  kind={kind} auth={'/'.join(cap.auth_modes)} "
            f"cloud_egress={cap.cloud_egress} models={cap.model_discovery} "
            f"usage={cap.usage_reporting}"
        )
        console.print(f"  [dim]{cap.notes}[/dim]")


@app.command("status")
@cli_command
def status(
    provider: Annotated[str, typer.Argument(help="Subscription provider id, e.g. codex")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a subscription provider's connection state (no secrets)."""
    cap = _require_subscription(provider)
    config = load_config(home=ensure_runtime_layout())
    _gate_privacy(config.privacy.external_ai_allowed, cap)

    from ragmonk.ai.runtime import resolve_runtime

    runtime = resolve_runtime(cap.provider_id, ai=config.ai)
    try:
        state = runtime.status()
    finally:
        runtime.close()
    if json_output:
        print_json(state.to_dict())
        return
    console.print(
        f"provider={state.provider_id} authenticated={state.authenticated} "
        f"account={state.account or '-'} runtime={state.runtime_version or '-'}"
    )
    if state.detail:
        console.print(f"[dim]{state.detail}[/dim]")


@app.command("login")
@cli_command
def login(
    provider: Annotated[str, typer.Argument(help="Subscription provider id, e.g. codex")],
) -> None:
    """Sign in to a subscription provider through its official runtime."""
    cap = _require_subscription(provider)
    config = load_config(home=ensure_runtime_layout())
    _gate_privacy(config.privacy.external_ai_allowed, cap)

    from ragmonk.ai.runtime import resolve_runtime

    runtime = resolve_runtime(cap.provider_id, ai=config.ai)
    try:
        state = runtime.login()
    finally:
        runtime.close()
    console.print(
        f"Signed in to [cyan]{state.provider_id}[/cyan] "
        f"as {state.account or 'the current account'}."
    )


@app.command("logout")
@cli_command
def logout(
    provider: Annotated[str, typer.Argument(help="Subscription provider id, e.g. codex")],
) -> None:
    """Sign out of a subscription provider via its official runtime."""
    cap = _require_subscription(provider)
    config = load_config(home=ensure_runtime_layout())

    from ragmonk.ai.runtime import resolve_runtime

    runtime = resolve_runtime(cap.provider_id, ai=config.ai)
    try:
        runtime.logout()
    finally:
        runtime.close()
    console.print(f"Signed out of [cyan]{cap.provider_id}[/cyan].")


@app.command("models")
@cli_command
def models(
    provider: Annotated[str, typer.Argument(help="Subscription provider id, e.g. codex")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the models a signed-in subscription provider offers."""
    cap = _require_subscription(provider)
    config = load_config(home=ensure_runtime_layout())
    _gate_privacy(config.privacy.external_ai_allowed, cap)

    from ragmonk.ai.runtime import resolve_runtime

    runtime = resolve_runtime(cap.provider_id, ai=config.ai)
    try:
        available = runtime.models()
    finally:
        runtime.close()
    if json_output:
        print_json({"provider": cap.provider_id, "models": available})
        return
    if not available:
        console.print(f"No models reported for [cyan]{cap.provider_id}[/cyan].")
        return
    for name in available:
        console.print(name)
