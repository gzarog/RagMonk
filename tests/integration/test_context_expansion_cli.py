"""``ragpilot search``'s Phase 9 context expansion end to end: a real
indexed multi-paragraph document, searched for a mid-document term, whose
JSON ``context`` carries the expected neighboring paragraph text -- and
proof that expansion never changes ranking order, only what is attached
to an already-selected hit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_project(root: Path) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "manual.md").write_text(
        "# Operations Manual\n\n"
        "## Startup\n\n"
        "Before starting the reactor, verify the coolant loop pressure "
        "reads within nominal range.\n\n"
        "The primary ignition sequence begins once all interlocks report "
        "green; this is the step operators refer to as FlywheelSpinup.\n\n"
        "After ignition, monitor the turbine RPM gauge for thirty seconds "
        "before releasing manual override.\n\n"
        "## Shutdown\n\n"
        "Shutdown follows the reverse sequence of startup, in strict "
        "order.\n"
    )


@pytest.fixture
def indexed_project(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CliRunner:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    # Each "## Startup" paragraph is well under the default 350-token
    # chunk budget, so they would otherwise all pack into one chunk
    # (``documents/chunker.py``'s greedy packing) and this fixture would
    # have no real sibling chunks to expand into -- a small max_tokens
    # (just above the largest single paragraph, ~35 tokens) forces each
    # paragraph to become its own chunk without splitting any of them
    # mid-sentence.
    for key, value in (
        ("documents.chunking.min_tokens", "1"),
        ("documents.chunking.overlap_tokens", "0"),
        ("documents.chunking.max_tokens", "40"),
    ):
        set_result = runner.invoke(app, ["config", "set", key, value])
        assert set_result.exit_code == 0, set_result.output
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    return runner


def _document_hits(payload: dict) -> list[dict]:  # noqa: ANN001 - test helper
    return [r for r in payload["results"] if r["kind"] == "document"]


def test_mid_document_hit_expands_with_real_neighbor_text(indexed_project: CliRunner) -> None:
    # "FlywheelSpinup" only appears in the middle paragraph -- its
    # immediate neighbors are the coolant-pressure paragraph before it
    # and the turbine-RPM paragraph after it.
    result = indexed_project.invoke(app, ["search", "FlywheelSpinup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    doc_hits = _document_hits(payload)
    assert doc_hits, payload
    context = doc_hits[0].get("context")
    assert context is not None, doc_hits[0]

    assert "FlywheelSpinup" in context["matched"]["text"]
    assert context["parent_heading"] is not None
    assert context["parent_heading"]["text"] == "Startup"
    previous_text = " ".join(p["text"] for p in context["previous"])
    next_text = " ".join(p["text"] for p in context["next"])
    assert "coolant loop pressure" in previous_text
    assert "turbine RPM gauge" in next_text


def test_context_absent_when_all_expansion_knobs_are_off(indexed_project: CliRunner) -> None:
    for key in (
        "search.context.parent_heading",
        "search.context.previous_chunks",
        "search.context.next_chunks",
    ):
        value = "false" if key.endswith("parent_heading") else "0"
        set_result = indexed_project.invoke(app, ["config", "set", key, value])
        assert set_result.exit_code == 0, set_result.output

    result = indexed_project.invoke(app, ["search", "FlywheelSpinup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    doc_hits = _document_hits(payload)
    assert doc_hits, payload
    assert "context" not in doc_hits[0]


def test_ranking_order_is_unaffected_by_context_expansion(indexed_project: CliRunner) -> None:
    with_expansion = indexed_project.invoke(app, ["search", "sequence", "--json"])
    assert with_expansion.exit_code == 0, with_expansion.output
    with_payload = json.loads(with_expansion.output)["data"]
    with_order = [(r["kind"], r["id"]) for r in with_payload["results"]]

    for key in (
        "search.context.parent_heading",
        "search.context.previous_chunks",
        "search.context.next_chunks",
    ):
        value = "false" if key.endswith("parent_heading") else "0"
        set_result = indexed_project.invoke(app, ["config", "set", key, value])
        assert set_result.exit_code == 0, set_result.output

    without_expansion = indexed_project.invoke(app, ["search", "sequence", "--json"])
    assert without_expansion.exit_code == 0, without_expansion.output
    without_payload = json.loads(without_expansion.output)["data"]
    without_order = [(r["kind"], r["id"]) for r in without_payload["results"]]

    assert with_order == without_order
    assert with_order  # the fixture query actually matches something
