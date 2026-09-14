"""``ragpilot search``'s snippet-block default output, its explicit
``--snippets``/``--table`` overrides, and ``search.output.fallback``'s
config-driven default/per-hit-degradation chain -- end to end via the
real CLI, extending ``test_retrieval_flow.py``'s fixture-project pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _unwrapped(output: str) -> str:
    """Rich's Console soft-wraps long unbroken tokens (a long absolute
    tmp_path with no spaces) mid-word at the test terminal's default
    width -- collapsing newlines back out makes a path-containment
    assertion robust to that instead of coupling the test to Rich's
    exact wrap width.
    """
    return output.replace("\n", "")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "lab_service.py").write_text(
        "class HdlAnalyzer:\n"
        "    def compute(self):\n"
        "        return 'ok'\n"
    )

    docs = root / "docs"
    docs.mkdir()
    # A long lead-in paragraph before the actual match, mirroring the
    # real-world report this feature was requested for: without a real
    # match-centered snippet, the naive body[:280] slice used to miss the
    # matching line entirely.
    padding = "General patient notes not relevant to this specific marker. " * 6
    (docs / "lab_notes.md").write_text(
        "# Lipid Panel\n\n"
        "## Results\n\n"
        f"{padding}HDL Cholesterol .......... 51 mg/dL. End of section.\n"
    )


@pytest.fixture
def indexed_project(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CliRunner:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    return runner


def test_default_search_shows_a_snippet_block(indexed_project: CliRunner) -> None:
    result = indexed_project.invoke(app, ["search", "HDL"])

    assert result.exit_code == 0, result.output
    assert "lab_notes.md" in _unwrapped(result.output)
    assert "Match:" in result.output
    assert "HDL Cholesterol" in result.output
    assert "51 mg/dL" in result.output
    # Markdown has no page concept -- falls back to the heading path
    # instead of a "Page:" line.
    assert "Section: Lipid Panel > Results" in result.output


def test_explicit_snippets_flag_matches_the_default(indexed_project: CliRunner) -> None:
    result = indexed_project.invoke(app, ["search", "HDL", "--snippets"])

    assert result.exit_code == 0, result.output
    assert "Match:" in result.output
    assert "HDL Cholesterol" in result.output


def test_table_flag_shows_the_plain_table_not_a_snippet(indexed_project: CliRunner) -> None:
    result = indexed_project.invoke(app, ["search", "HDL", "--table"])

    assert result.exit_code == 0, result.output
    assert "Match:" not in result.output
    assert "Kind" in result.output and "Tier" in result.output


def test_json_output_carries_the_real_match_snippet(indexed_project: CliRunner) -> None:
    result = indexed_project.invoke(app, ["search", "HDL", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    doc_hits = [r for r in payload["results"] if r["kind"] == "document"]
    assert doc_hits, payload
    assert "HDL Cholesterol" in doc_hits[0]["snippet"]
    assert doc_hits[0]["location"]["heading_path"] == ["Lipid Panel", "Results"]


def test_mixed_entity_and_document_hits_keep_entities_in_a_table(
    indexed_project: CliRunner,
) -> None:
    # "Hdl" matches both the HdlAnalyzer class (an entity hit) and the
    # lab_notes.md paragraph (a document hit) -- entity hits must keep
    # rendering as a plain table row, never a snippet block (confirmed
    # scope: snippets are document-only).
    result = indexed_project.invoke(app, ["search", "Hdl"])

    assert result.exit_code == 0, result.output
    assert "entity" in result.output
    assert "Match:" in result.output


def test_config_fallback_files_mode(indexed_project: CliRunner) -> None:
    set_result = indexed_project.invoke(
        app, ["config", "set", "search.output.fallback", "files"]
    )
    assert set_result.exit_code == 0, set_result.output

    result = indexed_project.invoke(app, ["search", "HDL"])

    assert result.exit_code == 0, result.output
    assert "Match:" not in result.output
    assert "lab_notes.md" in _unwrapped(result.output)


def test_config_fallback_json_mode_applies_without_the_json_flag(
    indexed_project: CliRunner,
) -> None:
    set_result = indexed_project.invoke(app, ["config", "set", "search.output.fallback", "json"])
    assert set_result.exit_code == 0, set_result.output

    result = indexed_project.invoke(app, ["search", "HDL"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["query"] == "HDL"


def test_explicit_snippets_flag_overrides_a_json_default(indexed_project: CliRunner) -> None:
    set_result = indexed_project.invoke(app, ["config", "set", "search.output.fallback", "json"])
    assert set_result.exit_code == 0, set_result.output

    result = indexed_project.invoke(app, ["search", "HDL", "--snippets"])

    assert result.exit_code == 0, result.output
    assert "Match:" in result.output


def test_config_set_rejects_an_unknown_fallback_mode(indexed_project: CliRunner) -> None:
    result = indexed_project.invoke(app, ["config", "set", "search.output.fallback", "bogus"])

    assert result.exit_code != 0
    assert "unknown search.output.fallback mode" in result.output
