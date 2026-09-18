"""Exact Tokenizer plan, Phase 4: ``ragmonk rebuild --fresh`` -- a
recoverable rebuild from source files that keeps the previous index as a
backup until the fresh rebuild succeeds, verifies source roots are
reachable first, and rolls back on failure.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_and_index(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "readme.md").write_text("# Title\n\nDocs about the exact tokenizer.\n")
    (source_dir / "a.py").write_text("def foo():\n    return 1\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    add_result = runner.invoke(app, ["source", "add", str(source_dir)])
    assert add_result.exit_code == 0, add_result.output
    match = SOURCE_ID_RE.search(add_result.output)
    assert match is not None, add_result.output
    assert runner.invoke(app, ["index"]).exit_code == 0
    return match.group(1)


def test_rebuild_fresh_reproduces_derived_state(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _add_and_index(runner, tmp_path, monkeypatch)
    docs_before = json.loads(runner.invoke(app, ["docs", "--json"]).output)["data"]

    result = runner.invoke(app, ["rebuild", "--fresh", "--yes", "--source", source_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["sources"][0]["failed"] == 0

    docs_after = json.loads(runner.invoke(app, ["docs", "--json"]).output)["data"]
    assert [d["title"] for d in docs_after["documents"]] == [
        d["title"] for d in docs_before["documents"]
    ]
    # No stray backup files were left behind on success.
    project_id = paths.project_id_for_path(tmp_path / "src")
    db_path = paths.project_db_path(project_id, ragmonk_home)
    assert not db_path.with_name(db_path.name + ".old").exists()


def test_rebuild_fresh_refuses_when_source_unreachable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _add_and_index(runner, tmp_path, monkeypatch)
    project_id = paths.project_id_for_path(tmp_path / "src")
    db_path = paths.project_db_path(project_id, ragmonk_home)
    original_bytes = db_path.read_bytes()

    # Make the registered source root unreachable.
    shutil.rmtree(tmp_path / "src")

    result = runner.invoke(app, ["rebuild", "--fresh", "--yes", "--source", source_id])
    assert result.exit_code != 0
    assert "not reachable" in result.output
    # The existing index is left completely untouched.
    assert db_path.read_bytes() == original_bytes


def test_rebuild_fresh_restores_previous_index_on_failure(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _add_and_index(runner, tmp_path, monkeypatch)
    project_id = paths.project_id_for_path(tmp_path / "src")
    db_path = paths.project_db_path(project_id, ragmonk_home)
    original_bytes = db_path.read_bytes()

    # Force the rebuild pass to blow up after the old index was moved aside.
    import ragmonk.ops.rebuild as rebuild_mod

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("simulated rebuild failure")

    monkeypatch.setattr(rebuild_mod, "run_source_pass", _boom)

    result = runner.invoke(app, ["rebuild", "--fresh", "--yes", "--source", source_id])
    assert result.exit_code != 0

    # The previously active index is restored intact, and no backup lingers.
    assert db_path.exists()
    assert db_path.read_bytes() == original_bytes
    assert not db_path.with_name(db_path.name + ".old").exists()
