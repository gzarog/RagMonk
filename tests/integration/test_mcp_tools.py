"""End-to-end: index a small mixed code+document project via the real CLI
(same fixture shape as ``test_retrieval_flow.py``), then call each of the
8 ``ragmonk_*`` MCP tool functions directly -- a FastMCP tool decorator
leaves the underlying coroutine callable as-is (see ``mcp/tools.py``'s
docstring), so no MCP transport/client is needed to exercise them.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.mcp import tools

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


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

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_animal_service.py").write_text(
        "from services.animal_service import AnimalService\n\n\n"
        "class AnimalServiceTests:\n"
        "    def test_bark(self):\n"
        "        service = AnimalService()\n"
        "        service.bark_loudly()\n"
    )

    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "# API Reference\n\n"
        "The services.animal_service.AnimalService class documents the "
        "animal service. See services/animal_service.py for the "
        "implementation.\n"
    )


@pytest.fixture
def indexed_project(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    add_result = runner.invoke(app, ["source", "add", str(root)])
    assert add_result.exit_code == 0, add_result.output
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    return root


def test_ragmonk_explore(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_explore(query="what breaks if AnimalService changes?"))

    assert result.ok is True
    assert result.error is None
    assert result.schema_version == "1"
    assert result.intent == "impact"
    assert result.answer_context
    assert any(
        e.qualified_name == "services.animal_service.AnimalService" for e in result.entities
    )
    assert "tests.test_animal_service.AnimalServiceTests.test_bark" in result.tests
    assert result.documents or result.evidence
    assert any(rel.relationship == "calls" for rel in result.relationships)
    # Requirements/Incidents have no classifier yet (Phases 1-5): explore
    # always surfaces that scope limitation as a warning rather than
    # silently returning empty categories with no explanation.
    assert any("Requirements/Incidents" in w for w in result.warnings)


def test_ragmonk_explore_document_intent(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_explore(query="documents about animal service"))

    assert result.ok is True
    assert result.intent == "document"
    assert result.documents


def test_ragmonk_search(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_search(query="AnimalService"))

    assert result.ok is True
    entity_hits = [r for r in result.results if r.kind == "entity"]
    assert entity_hits, result.results
    assert entity_hits[0].tier == "exact_symbol"
    assert entity_hits[0].title == "services.animal_service.AnimalService"


def test_ragmonk_symbol(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_symbol(name="AnimalService"))

    assert result.ok is True
    assert len(result.matches) == 1
    assert result.matches[0].qualified_name == "services.animal_service.AnimalService"
    assert result.matches[0].kind == "class"


def test_ragmonk_callers(indexed_project: Path) -> None:
    result = asyncio.run(
        tools.ragmonk_callers(name="services.animal_service.AnimalService.bark_loudly")
    )

    assert result.ok is True
    assert len(result.matches) == 1
    assert len(result.edges) == 2
    assert all(edge.relationship_type == "calls" for edge in result.edges)


def test_ragmonk_callees(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_callees(name="consumers.dog_consumer.DogConsumer.handle"))

    assert result.ok is True
    assert any(edge.relationship_type == "calls" for edge in result.edges)


def test_ragmonk_impact(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_impact(name="AnimalService"))

    assert result.ok is True
    assert result.found is True
    assert result.defined and result.defined[0].path.endswith("animal_service.py")
    assert "consumers.dog_consumer.DogConsumer.handle" in result.callers
    assert result.tests == ["tests.test_animal_service.AnimalServiceTests.test_bark"]
    assert result.documentation
    assert result.blast_radius in {"LOW", "MEDIUM", "HIGH"}
    assert result.confidence is not None
    assert result.confidence.code_references is not None


def test_ragmonk_impact_unknown_symbol_is_found_false_not_an_error(
    indexed_project: Path,
) -> None:
    result = asyncio.run(tools.ragmonk_impact(name="NoSuchSymbolAtAll"))

    assert result.ok is True
    assert result.error is None
    assert result.found is False


def test_ragmonk_documents(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_documents())

    assert result.ok is True
    assert any(d.path.endswith("api.md") for d in result.documents)
    assert all(d.status == "indexed" for d in result.documents)


def test_ragmonk_documents_unknown_source_is_a_structured_error(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_documents(source_id="src_does_not_exist"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "UsageError"
    assert "no such source" in result.error.message


def test_ragmonk_status(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_status())

    assert result.ok is True
    assert result.totals is not None
    assert result.totals.by_status.get("indexed", 0) == 4
    assert len(result.sources) == 1


def test_ragmonk_explore_respects_context_budget(indexed_project: Path) -> None:
    unbounded = asyncio.run(
        tools.ragmonk_explore(query="what breaks if AnimalService changes?")
    )
    bounded = asyncio.run(
        tools.ragmonk_explore(
            query="what breaks if AnimalService changes?", max_chars=1, max_files=1
        )
    )

    assert unbounded.ok is True
    assert bounded.ok is True
    total_snippet_chars = sum(len(item.snippet) for item in bounded.evidence)
    assert total_snippet_chars <= 1
    assert any("max_chars=1" in w or "max_files=1" in w for w in bounded.warnings)


def test_bad_input_is_a_structured_error_not_a_crash(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_search(query="   "))

    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "UsageError"
    assert result.results == []


def test_unknown_symbol_is_an_empty_result_not_an_error(indexed_project: Path) -> None:
    result = asyncio.run(tools.ragmonk_symbol(name="NoSuchSymbolAtAll"))

    assert result.ok is True
    assert result.error is None
    assert result.matches == []


def test_timeout_returns_structured_error_not_a_hang(
    indexed_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGMONK_MCP__REQUEST_TIMEOUT_SECONDS", "0.05")

    from ragmonk.retrieval import lexical

    original_search = lexical.search

    def slow_search(*args: object, **kwargs: object) -> list[object]:
        time.sleep(0.5)
        return original_search(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lexical, "search", slow_search)

    async def _timed_call() -> tuple[tools.SearchOutput, float]:
        # Timed from *inside* the event loop, around the awaited call only
        # -- ``asyncio.run`` itself still joins the abandoned background
        # thread during its own shutdown once ``slow_search`` eventually
        # returns, which is irrelevant to what this test checks: that the
        # tool call itself did not wait for it.
        start = time.monotonic()
        result = await tools.ragmonk_search(query="AnimalService")
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(_timed_call())

    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "timeout"
    assert "0.05" in result.error.message
    # Well under the 0.5s sleep -- proves the call returned on the
    # timeout, not once the slow call finally completed.
    assert elapsed < 0.3


def test_ragmonk_ask_returns_the_mocked_provider_answer_with_real_evidence(
    indexed_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.ai.base import AiAnswer, AiRequest, AiUsage
    from ragmonk.cli import ask as ask_cli

    class _FakeProvider:
        def answer(self, request: AiRequest) -> AiAnswer:
            assert request.evidence != []
            return AiAnswer(
                text="Answer grounded in evidence.",
                provider="fake",
                model="fake-1",
                usage=AiUsage(input_tokens=1, output_tokens=1),
            )

    monkeypatch.setattr(ask_cli, "_build_provider", lambda ctx: _FakeProvider())

    result = asyncio.run(
        tools.ragmonk_ask(question="what breaks if AnimalService changes?")
    )

    assert result.ok is True
    assert result.answer is not None
    assert result.answer.text == "Answer grounded in evidence."
    assert result.answer.provider == "fake"
    assert result.evidence != []


def test_ragmonk_ask_without_a_configured_provider_is_a_structured_error(
    indexed_project: Path,
) -> None:
    result = asyncio.run(tools.ragmonk_ask(question="what does AnimalService do?"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "AiNotConfiguredError"
    assert result.answer is None


def test_ragmonk_ask_blocked_by_privacy_flag_is_a_structured_error(
    indexed_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGMONK_AI__PROVIDER", "openai")
    monkeypatch.setenv("RAGMONK_AI__MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = asyncio.run(tools.ragmonk_ask(question="what does AnimalService do?"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.type == "AiPrivacyBlockedError"
