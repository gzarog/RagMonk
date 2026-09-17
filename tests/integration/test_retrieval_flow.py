"""End-to-end: index a small mixed code+document project (extending the
Phase 4 cross-domain-linking fixture with a caller and a test-named
caller), then verify ``ragmonk search``, ``ragmonk impact`` and
``ragmonk explore`` (incl. ``--json``) via the real CLI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "animal_service.py").write_text(
        "class AnimalService:\n"
        "    def bark_loudly(self):\n"
        "        return 'WOOF'\n"
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
    (docs / "api.md") .write_text(
        "# API Reference\n\n"
        "The services.animal_service.AnimalService class documents the "
        "animal service. See services/animal_service.py for the "
        "implementation.\n"
    )


def test_search_impact_and_explore_end_to_end(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    assert "linked=" in index_result.output

    # -- search --------------------------------------------------------
    search_result = runner.invoke(app, ["search", "AnimalService", "--json"])
    assert search_result.exit_code == 0, search_result.output
    search_payload = json.loads(search_result.output)["data"]
    assert search_payload["query"] == "AnimalService"
    entity_hits = [r for r in search_payload["results"] if r["kind"] == "entity"]
    assert entity_hits, search_payload
    assert entity_hits[0]["tier"] == "exact_symbol"
    assert entity_hits[0]["title"] == "services.animal_service.AnimalService"

    search_text = runner.invoke(app, ["search", "AnimalService"])
    assert search_text.exit_code == 0
    assert "entity" in search_text.output

    # -- impact ----------------------------------------------------------
    impact_result = runner.invoke(app, ["impact", "AnimalService", "--json"])
    assert impact_result.exit_code == 0, impact_result.output
    impact_payload = json.loads(impact_result.output)["data"]
    assert impact_payload["found"] is True
    assert impact_payload["defined"][0]["path"].endswith("animal_service.py")
    assert "consumers.dog_consumer.DogConsumer.handle" in impact_payload["callers"]
    assert (
        "tests.test_animal_service.AnimalServiceTests.test_bark" in impact_payload["callers"]
    )
    assert impact_payload["tests"] == [
        "tests.test_animal_service.AnimalServiceTests.test_bark"
    ]
    assert impact_payload["documentation"] != []
    assert impact_payload["confidence"]["code_references"] is not None
    assert impact_payload["blast_radius"] in {"LOW", "MEDIUM", "HIGH"}
    # 2 distinct callers (DogConsumer.handle, AnimalServiceTests.test_bark)
    # + at least 1 distinct linked document.
    assert impact_payload["blast_radius_score"] >= 3

    impact_text = runner.invoke(app, ["impact", "AnimalService"])
    assert impact_text.exit_code == 0
    assert "Blast radius:" in impact_text.output

    impact_missing = runner.invoke(app, ["impact", "NoSuchSymbol"])
    assert impact_missing.exit_code == 0
    assert "No entities found" in impact_missing.output

    # -- explore -----------------------------------------------------------
    explore_result = runner.invoke(
        app, ["explore", "what breaks if AnimalService changes?", "--json"]
    )
    assert explore_result.exit_code == 0, explore_result.output
    explore_payload = json.loads(explore_result.output)["data"]
    assert explore_payload["intent"] == "impact"
    assert explore_payload["strategies"] == [
        "identifier",
        "graph_callers",
        "graph_callees",
        "tests",
        "documents",
    ]
    assert any(
        s["qualified_name"] == "services.animal_service.AnimalService"
        for s in explore_payload["symbols"]
    )
    assert "tests.test_animal_service.AnimalServiceTests.test_bark" in explore_payload["tests"]
    assert explore_payload["documents"] != [] or explore_payload["evidence"] != []
    assert explore_payload["requirements"] == []
    assert explore_payload["incidents"] == []
    assert isinstance(explore_payload["evidence_truncated"], bool)

    explore_text = runner.invoke(app, ["explore", "who calls AnimalService"])
    assert explore_text.exit_code == 0
    assert "Relevant symbols" in explore_text.output

    explore_docs = runner.invoke(app, ["explore", "documents about animal service", "--json"])
    assert explore_docs.exit_code == 0, explore_docs.output
    explore_docs_payload = json.loads(explore_docs.output)["data"]
    assert explore_docs_payload["intent"] == "document"
    assert explore_docs_payload["documents"] != []
