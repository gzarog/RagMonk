"""End-to-end: index a small multi-file, multi-language project via the
real CLI, then exercise ``symbol``/``callers``/``callees``/``references``
(including ``--json``), and confirm a syntactically-broken file is
isolated (recorded failed, exit code 6) without stopping the rest --
Phase 1's "poisoned file" pattern applied to Phase 2's real processor.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.cli.main import app
from ragmonk.core.errors import EXIT_INDEXING_PARTIAL_FAILURE

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_sample_project(root: Path) -> None:
    pyservice = root / "pyservice"
    pyservice.mkdir(parents=True)
    (pyservice / "utils.py").write_text("def helper():\n    return 42\n")
    (pyservice / "app.py").write_text(
        "from pyservice.utils import helper\n\n\ndef main():\n    return helper()\n"
    )

    webapp = root / "webapp"
    webapp.mkdir()
    (webapp / "index.js").write_text(
        "function add(a, b) {\n  return a + b;\n}\n\n"
        "function run() {\n  return add(1, 2);\n}\n"
    )

    # Syntactically broken -- unmatched parenthesis -- must be isolated,
    # not crash the run.
    (root / "broken.py").write_text("def broken(\n    pass\n")


def _touch_forward(path: Path) -> None:
    current = path.stat().st_mtime
    os.utime(path, (current + 10, current + 10))


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    root = tmp_path / "sample_project"
    _write_sample_project(root)
    return root


@pytest.fixture(autouse=True)
def _force_permanent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real backoff needs MAX_ATTEMPTS (5) index runs before a failure is
    # permanent; force it on the first attempt so these tests don't need
    # to invoke ``index`` five times to observe broken.py land in FAILED.
    monkeypatch.setattr(coordinator_module.retry, "is_permanent", lambda attempt: True)


def test_index_multi_language_project_isolates_broken_file(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sample_project.parent)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, sample_project)

    result = runner.invoke(app, ["index"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    assert totals.get("failed", 0) == 1
    assert totals.get("indexed", 0) == 3  # utils.py, app.py, index.js


def test_symbol_lookup_json(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sample_project.parent)
    runner.invoke(app, ["init"])
    _add_source(runner, sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["symbol", "helper", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1"
    matches = payload["data"]["matches"]
    assert len(matches) == 1
    assert matches[0]["qualified_name"].endswith("helper")
    assert matches[0]["kind"] == "function"


def test_callers_callees_references_json(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sample_project.parent)
    runner.invoke(app, ["init"])
    _add_source(runner, sample_project)
    runner.invoke(app, ["index"])
    # Cross-file resolution only ever sees files "already indexed" by an
    # earlier pass (resolver.py's documented rule) -- os.walk's directory
    # order is not guaranteed, so app.py's call to utils.helper() may be
    # unresolved on this very first pass if utils.py happened to be
    # scanned after it. Touching app.py and reindexing forces a second
    # pass that runs strictly after utils.py is guaranteed indexed,
    # without depending on filesystem walk order for the assertion below.
    app_py = sample_project / "pyservice" / "app.py"
    app_py.write_text(app_py.read_text() + "\n# reindex\n")
    _touch_forward(app_py)
    runner.invoke(app, ["index"])

    callers_result = runner.invoke(app, ["callers", "helper", "--json"])
    assert callers_result.exit_code == 0, callers_result.output
    callers_payload = json.loads(callers_result.output)["data"]
    assert len(callers_payload["edges"]) == 1
    caller_edge = callers_payload["edges"][0]
    assert caller_edge["relationship_type"] == "calls"
    assert caller_edge["confidence"] in ("exact", "high", "medium")

    callees_result = runner.invoke(app, ["callees", "main", "--json"])
    assert callees_result.exit_code == 0, callees_result.output
    callees_payload = json.loads(callees_result.output)["data"]
    assert len(callees_payload["edges"]) == 1
    assert callees_payload["edges"][0]["target_entity_id"] is not None

    references_result = runner.invoke(app, ["references", "helper", "--json"])
    assert references_result.exit_code == 0, references_result.output
    references_payload = json.loads(references_result.output)["data"]
    assert any(e["relationship_type"] == "calls" for e in references_payload["edges"])


def test_javascript_same_file_call_is_exact_confidence(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sample_project.parent)
    runner.invoke(app, ["init"])
    _add_source(runner, sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["callees", "run", "--json"])
    assert result.exit_code == 0, result.output
    edges = json.loads(result.output)["data"]["edges"]
    assert len(edges) == 1
    assert edges[0]["confidence"] == "exact"


def test_tree_sitter_parse_exception_is_isolated_not_crashing(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse call that itself raises (not just a recoverable syntax
    error) must be caught by the coordinator like any other processor
    exception, isolated to that one file, and not crash ``ragmonk index``.
    """
    import ragmonk.code.processor as processor_module

    original_parse = processor_module.parse

    def flaky_parse(source: bytes, language: str):  # type: ignore[no-untyped-def]
        if language == "javascript":
            raise RuntimeError("simulated tree-sitter crash")
        return original_parse(source, language)

    monkeypatch.setattr(processor_module, "parse", flaky_parse)
    monkeypatch.chdir(sample_project.parent)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, sample_project)

    result = runner.invoke(app, ["index"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE

    status = runner.invoke(app, ["status", "--json"])
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    # broken.py (syntax error) + index.js (simulated parser crash) both
    # isolated as failed; the two Python files still index successfully.
    assert totals.get("failed", 0) == 2
    assert totals.get("indexed", 0) == 2


def test_unknown_symbol_returns_empty_without_error(
    ragmonk_home: Path, runner: CliRunner, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sample_project.parent)
    runner.invoke(app, ["init"])
    _add_source(runner, sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["symbol", "does_not_exist_anywhere", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["data"]["matches"] == []


def test_callers_found_even_when_definition_indexed_after_the_call_site(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller processed before its target's defining file exists gets a
    ``target_symbol``-only (unresolved) relationship -- see
    ``code/resolver.py``. ``callers`` must still surface it once the
    definition is indexed, rather than only ever seeing edges resolved at
    the moment they were first recorded (scan order is not guaranteed --
    see ``sources/scanner.py`` -- so this cannot be left to chance).
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "consumer.py").write_text(
        "class DogConsumer:\n    def handle(self):\n        return AnimalService()\n"
    )
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    # At this point "AnimalService" has no definition anywhere: the call
    # is recorded unresolved (target_symbol="AnimalService", no entity).
    unresolved = runner.invoke(app, ["callers", "AnimalService", "--json"])
    assert unresolved.exit_code == 0
    unresolved_edges = json.loads(unresolved.output)["data"]["edges"]
    assert unresolved_edges, unresolved.output
    assert all(e["target_entity_id"] is None for e in unresolved_edges), unresolved_edges
    assert any(e["source_location"].endswith("consumer.py:3") for e in unresolved_edges)

    (root / "animal_service.py").write_text(
        "class AnimalService:\n    pass\n"
    )
    assert runner.invoke(app, ["index"]).exit_code == 0

    resolved = runner.invoke(app, ["callers", "AnimalService", "--json"])
    assert resolved.exit_code == 0
    edges = json.loads(resolved.output)["data"]["edges"]
    assert any(e["source_location"].endswith("consumer.py:3") for e in edges), edges


def test_callers_found_by_qualified_name_when_unresolved_row_stored_bare(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same scenario as above, but querying ``callers`` by the target's
    fully-qualified name rather than its bare name. An unresolved
    relationship is always stored under the *bare* name
    ``code/resolver.py`` fell back to (``target_symbol="bark_loudly"``,
    never the qualified query text) -- the merge in
    ``code/graph.py``/``retrieval/graph.py`` must search under both the
    literal query and each matched entity's bare name, or a
    qualified-name query finds nothing despite the bare-name query
    (previous test) finding the same edge.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "consumer.py").write_text(
        "class DogConsumer:\n"
        "    def handle(self):\n"
        "        service = AnimalService()\n"
        "        return service.bark_loudly()\n"
    )
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    (root / "animal_service.py").write_text(
        "class AnimalService:\n    def bark_loudly(self):\n        return 'WOOF'\n"
    )
    assert runner.invoke(app, ["index"]).exit_code == 0

    resolved = runner.invoke(
        app, ["callers", "animal_service.AnimalService.bark_loudly", "--json"]
    )
    assert resolved.exit_code == 0
    edges = json.loads(resolved.output)["data"]["edges"]
    assert any(e["source_location"].endswith("consumer.py:4") for e in edges), edges
