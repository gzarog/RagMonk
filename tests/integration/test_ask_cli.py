"""End-to-end ``ragmonk ask``: real retrieval (index a small mixed code+
document project, run the real query planner + evidence assembly) with
the AI provider itself mocked -- zero real network calls, per Phase 9's
testing constraint. Also proves the clear-error paths (no provider
configured, privacy flag off, provider call failure) map to the right
exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.ai.base import AiAnswer, AiProvider, AiRequest, AiUsage
from ragmonk.cli import ask as ask_cli
from ragmonk.cli.main import app
from ragmonk.core.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_GENERIC_FAILURE,
    EXIT_SECURITY_RESTRICTION,
)


class _FakeProvider:
    def __init__(self, *, text: str = "AnimalService barks loudly.") -> None:
        self._text = text
        self.received: AiRequest | None = None

    def answer(self, request: AiRequest) -> AiAnswer:
        self.received = request
        return AiAnswer(
            text=self._text,
            provider="fake",
            model="fake-model-1",
            usage=AiUsage(input_tokens=100, output_tokens=10),
        )


class _BrokenProvider:
    def answer(self, request: AiRequest) -> AiAnswer:
        raise RuntimeError("simulated provider crash")


def _write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "animal_service.py").write_text(
        "class AnimalService:\n    def bark_loudly(self):\n        return 'WOOF'\n"
    )
    consumers = root / "consumers"
    consumers.mkdir()
    (consumers / "dog_consumer.py").write_text(
        "from services.animal_service import AnimalService\n\n\n"
        "class DogConsumer:\n"
        "    def handle(self):\n"
        "        service = AnimalService()\n"
        "        return service.bark_loudly()\n"
    )


@pytest.fixture
def indexed_project(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0
    return root


def test_ask_assembles_real_evidence_and_returns_the_mocked_answer(
    indexed_project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeProvider()
    monkeypatch.setattr(ask_cli, "_build_provider", lambda ctx: fake)

    result = runner.invoke(
        app, ["ask", "what breaks if AnimalService changes?", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]

    assert payload["question"] == "what breaks if AnimalService changes?"
    assert payload["answer"]["text"] == "AnimalService barks loudly."
    assert payload["answer"]["provider"] == "fake"
    assert payload["answer"]["model"] == "fake-model-1"
    assert payload["answer"]["usage"]["input_tokens"] == 100
    assert payload["evidence"] != []
    assert payload["intent"] == "impact"

    # The provider actually received the real, deterministically-retrieved
    # evidence -- not a stub -- so the answer stays auditable.
    assert fake.received is not None
    assert fake.received.evidence != []
    assert any("dog_consumer" in item["path"].lower() for item in fake.received.evidence)


def test_ask_text_output_prints_the_answer(
    indexed_project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ask_cli, "_build_provider", lambda ctx: _FakeProvider(text="Hello there."))

    result = runner.invoke(app, ["ask", "what does AnimalService do?"])
    assert result.exit_code == 0, result.output
    assert "Hello there." in result.output


def test_ask_fails_clearly_when_no_provider_is_configured(
    indexed_project: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["ask", "what does AnimalService do?"])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "ai.provider" in result.output


def test_ask_fails_clearly_when_privacy_flag_blocks_a_cloud_provider(
    indexed_project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGMONK_AI__PROVIDER", "openai")
    monkeypatch.setenv("RAGMONK_AI__MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # privacy.external_ai_allowed defaults to false -- deliberately not set.

    result = runner.invoke(app, ["ask", "what does AnimalService do?"])
    assert result.exit_code == EXIT_SECURITY_RESTRICTION
    assert "external_ai_allowed" in result.output


def test_ask_maps_a_provider_failure_to_a_generic_failure_exit_code(
    indexed_project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ask_cli, "_build_provider", lambda ctx: _BrokenProvider())

    result = runner.invoke(app, ["ask", "what does AnimalService do?"])
    assert result.exit_code == EXIT_GENERIC_FAILURE
    assert "simulated provider crash" in result.output


def test_ask_provider_raising_ai_provider_error_directly_is_not_double_wrapped(
    indexed_project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.ai.base import AiProviderError

    class _RudeProvider:
        def answer(self, request: AiRequest) -> AiAnswer:
            raise AiProviderError("upstream said no")

    monkeypatch.setattr(ask_cli, "_build_provider", lambda ctx: _RudeProvider())

    result = runner.invoke(app, ["ask", "what does AnimalService do?"])
    assert result.exit_code == EXIT_GENERIC_FAILURE
    assert "upstream said no" in result.output


def test_ask_provider_protocol_is_satisfied_by_fake_provider() -> None:
    fake: AiProvider = _FakeProvider()
    assert hasattr(fake, "answer")
