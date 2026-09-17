"""``ragmonk doctor``'s AI diagnostics section (subscription plan,
Phase 4): offline, secret-free, and never a FAIL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ragmonk.cli import doctor
from ragmonk.core.config import RagMonkConfig


def _ctx(config: RagMonkConfig) -> SimpleNamespace:
    # _ai_section only reads ctx.config; a namespace is enough.
    return SimpleNamespace(config=config)


def test_ai_section_default_provider_is_ok_only() -> None:
    section = doctor._ai_section(_ctx(RagMonkConfig()))
    assert section.name == "AI"
    assert [c.status for c in section.checks] == ["ok"]
    assert "provider=none" in section.checks[0].detail


def test_ai_section_warns_when_cloud_provider_without_privacy_flag() -> None:
    config = RagMonkConfig.model_validate({"ai": {"provider": "codex"}})
    section = doctor._ai_section(_ctx(config))
    statuses = {c.name: c.status for c in section.checks}
    assert statuses["privacy"] == "warn"


def test_ai_section_never_prints_secrets_and_is_never_fail() -> None:
    config = RagMonkConfig.model_validate(
        {"ai": {"provider": "codex"}, "privacy": {"external_ai_allowed": True}}
    )
    section = doctor._ai_section(_ctx(config))
    assert all(c.status in ("ok", "warn") for c in section.checks)
    joined = " ".join(c.detail for c in section.checks).lower()
    assert "token" not in joined or "shadow" in joined  # only ever names env var *names*


def test_subscription_runtime_check_codex_reports_path_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._subscription_runtime_check("codex")
    assert result.status == "warn"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/codex")
    result = doctor._subscription_runtime_check("codex")
    assert result.status == "ok"


def test_subscription_runtime_check_copilot_warns_without_sdk() -> None:
    # The optional SDK is not installed in CI -> a WARN, not a crash.
    result = doctor._subscription_runtime_check("github_copilot")
    assert result.status == "warn"
