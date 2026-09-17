"""``ragmonk rebuild``: proves wiping a project's ``knowledge.db`` and
re-indexing from scratch reproduces the same derived state -- the
blueprint's "source files = truth, RagMonk DB = rebuildable derived
state" principle, exercised for real rather than merely asserted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _write_project(tmp_path: Path) -> Path:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def foo():\n    return bar()\n\ndef bar():\n    return 1\n")
    (source_dir / "readme.md").write_text("# Title\n\nDocs about foo().\n")
    return source_dir


def test_rebuild_reproduces_equivalent_derived_state(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    add_result = runner.invoke(app, ["source", "add", str(source_dir)])
    assert add_result.exit_code == 0
    match = SOURCE_ID_RE.search(add_result.output)
    assert match is not None, add_result.output
    source_id = match.group(1)
    assert runner.invoke(app, ["index"]).exit_code == 0

    status_before = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    symbol_before = json.loads(runner.invoke(app, ["symbol", "foo", "--json"]).output)["data"]
    callers_before = json.loads(runner.invoke(app, ["callers", "bar", "--json"]).output)["data"]
    docs_before = json.loads(runner.invoke(app, ["docs", "--json"]).output)["data"]

    project_id = paths.project_id_for_path(source_dir)
    db_path = paths.project_db_path(project_id, ragmonk_home)
    original_bytes = db_path.stat().st_size
    assert original_bytes > 0

    rebuild_result = runner.invoke(app, ["rebuild", "--source", source_id, "--json"])
    assert rebuild_result.exit_code == 0, rebuild_result.output
    rebuild_payload = json.loads(rebuild_result.output)["data"]
    assert rebuild_payload["sources"][0]["failed"] == 0
    assert rebuild_payload["sources"][0]["indexed"] == 2

    status_after = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    symbol_after = json.loads(runner.invoke(app, ["symbol", "foo", "--json"]).output)["data"]
    callers_after = json.loads(runner.invoke(app, ["callers", "bar", "--json"]).output)["data"]
    docs_after = json.loads(runner.invoke(app, ["docs", "--json"]).output)["data"]

    assert status_after["totals"]["by_status"] == status_before["totals"]["by_status"]
    assert status_after["totals"]["metrics"]["symbols_created"] == (
        status_before["totals"]["metrics"]["symbols_created"]
    )
    assert status_after["totals"]["metrics"]["relationships_created"] == (
        status_before["totals"]["metrics"]["relationships_created"]
    )
    assert status_after["totals"]["metrics"]["documents_processed"] == (
        status_before["totals"]["metrics"]["documents_processed"]
    )

    # Entity/edge ids are freshly generated (uuid4-based) on every index
    # run, so compare on stable, content-derived fields instead of raw
    # equality.
    assert [m["qualified_name"] for m in symbol_after["matches"]] == [
        m["qualified_name"] for m in symbol_before["matches"]
    ]
    assert [
        (e["relationship_type"], e["target_symbol"], e["confidence"])
        for e in callers_after["edges"]
    ] == [
        (e["relationship_type"], e["target_symbol"], e["confidence"])
        for e in callers_before["edges"]
    ]
    assert [d["title"] for d in docs_after["documents"]] == [
        d["title"] for d in docs_before["documents"]
    ]


def test_rebuild_all_sources_when_no_source_given(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_a = tmp_path / "a"
    source_a.mkdir()
    (source_a / "m.py").write_text("x = 1\n")
    source_b = tmp_path / "b"
    source_b.mkdir()
    (source_b / "n.py").write_text("y = 2\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_a)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_b)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    result = runner.invoke(app, ["rebuild", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert len(payload["sources"]) == 2
