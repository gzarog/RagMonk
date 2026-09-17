"""``ragmonk ai ...`` command group (subscription plan, Phase 1).

These prove the *lifecycle plumbing* without any live inference: listing
providers, the privacy gate firing before a runtime is contacted, and the
Phase 1 "runtime unavailable" outcome for subscription providers whose
adapter has not shipped yet. All exit codes stay on this project's
existing conventions.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_INVALID_ARGUMENTS,
    EXIT_SECURITY_RESTRICTION,
)


def _set(runner: CliRunner, key: str, value: str) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == 0, result.output


def test_providers_lists_every_known_provider(ragmonk_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["ai", "providers"])
    assert result.exit_code == 0, result.output
    for name in ("openai", "anthropic", "ollama", "openai_compatible", "codex", "github_copilot"):
        assert name in result.output


def test_providers_json_is_structured(ragmonk_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["ai", "providers", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = {p["provider_id"] for p in payload["data"]["providers"]}
    assert {"codex", "github_copilot"} <= ids


def test_status_on_non_subscription_provider_is_invalid_arguments(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["ai", "status", "openai"])
    assert result.exit_code == EXIT_INVALID_ARGUMENTS


def test_login_unknown_provider_is_invalid_arguments(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["ai", "login", "not-a-provider"])
    assert result.exit_code == EXIT_INVALID_ARGUMENTS


def test_codex_status_is_blocked_when_privacy_flag_is_off(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    # privacy.external_ai_allowed defaults to false -> a cloud runtime must
    # not even be contacted.
    result = runner.invoke(app, ["ai", "status", "codex"])
    assert result.exit_code == EXIT_SECURITY_RESTRICTION


def test_codex_status_runtime_unavailable_once_privacy_allowed(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    _set(runner, "privacy.external_ai_allowed", "true")
    result = runner.invoke(app, ["ai", "status", "codex"])
    # Phase 1 ships no codex runtime, so past the privacy gate the next
    # honest outcome is "runtime unavailable" -- a config-class error.
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_codex_login_runtime_unavailable_once_privacy_allowed(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    _set(runner, "privacy.external_ai_allowed", "true")
    result = runner.invoke(app, ["ai", "login", "codex"])
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_github_copilot_models_runtime_unavailable_once_privacy_allowed(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    _set(runner, "privacy.external_ai_allowed", "true")
    result = runner.invoke(app, ["ai", "models", "github_copilot"])
    assert result.exit_code == EXIT_CONFIG_ERROR
