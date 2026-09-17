"""``ragmonk status --json``'s Phase 8 metrics block: real counts against
a known small indexed project, not just "doesn't crash".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths


def test_status_metrics_reflect_known_project(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    # One code file: 2 functions (symbols), one CALLS relationship.
    (source_dir / "a.py").write_text("def foo():\n    return bar()\n\ndef bar():\n    return 1\n")
    # One document file.
    (source_dir / "readme.md").write_text("# Title\n\nSome text about foo().\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]

    assert len(payload["sources"]) == 1
    source_row = payload["sources"][0]
    # 2 functions plus the module-level namespace entity Phase 2's Python
    # extractor emits for the file itself -- see code/queries/python.scm.
    assert source_row["metrics"]["symbols_created"] == 3
    assert source_row["metrics"]["documents_processed"] == 1
    assert source_row["metrics"]["database_size_bytes"] > 0

    totals_metrics = payload["totals"]["metrics"]
    assert totals_metrics["files_discovered"] == 2
    assert totals_metrics["files_indexed"] == 2
    assert totals_metrics["files_failed"] == 0
    assert totals_metrics["symbols_created"] == 3
    assert totals_metrics["relationships_created"] >= 1
    assert totals_metrics["documents_processed"] == 1
    assert totals_metrics["index_queue_depth"] == 0

    expected_bytes = paths.sources_db_path(ragmonk_home).stat().st_size
    project_id = paths.project_id_for_path(source_dir)
    expected_bytes += paths.project_db_path(project_id, ragmonk_home).stat().st_size
    assert totals_metrics["database_size_bytes"] == expected_bytes


def test_status_metrics_zero_for_project_with_no_documents_or_calls(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    result = runner.invoke(app, ["status", "--json"])
    payload = json.loads(result.output)["data"]
    metrics = payload["totals"]["metrics"]
    assert metrics["documents_processed"] == 0
    assert metrics["relationships_created"] == 0
