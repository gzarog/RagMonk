from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _files_conn(home: Path, source_path: Path):  # noqa: ANN201 - test helper
    project_id = paths.project_id_for_path(source_path)
    conn = connect(paths.project_db_path(project_id, home))
    apply_migrations(conn, "knowledge")
    return conn


def test_full_index_and_status_flow(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("print('a')")
    (source_dir / "b.md").write_text("# hello")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.output)
    assert payload["schema_version"] == "1"
    totals = payload["data"]["totals"]["by_status"]
    assert totals["indexed"] == 2
    assert payload["data"]["totals"]["queue_depth"] == 0

    conn = _files_conn(ragmonk_home, source_dir)
    try:
        before = {f.path: f.generation for f in files_repo.list_by_source(conn, source_id)}
    finally:
        conn.close()

    # Modify one file only; bump mtime explicitly so the change is detected
    # deterministically without depending on filesystem timestamp resolution.
    a_path = source_dir / "a.py"
    a_path.write_text("print('a-changed')")
    os.utime(a_path, (a_path.stat().st_mtime + 10, a_path.stat().st_mtime + 10))

    assert runner.invoke(app, ["index"]).exit_code == 0

    conn = _files_conn(ragmonk_home, source_dir)
    try:
        after = {f.path: f.generation for f in files_repo.list_by_source(conn, source_id)}
    finally:
        conn.close()

    changed_paths = [p for p in before if after[p] != before[p]]
    assert changed_paths == [str(a_path.resolve())]

    status_after = runner.invoke(app, ["status", "--json"])
    totals_after = json.loads(status_after.output)["data"]["totals"]["by_status"]
    assert totals_after["indexed"] == 2

    # Delete a file: the next index run must reconcile it away.
    (source_dir / "b.md").unlink()
    assert runner.invoke(app, ["index"]).exit_code == 0
    status_final = runner.invoke(app, ["status", "--json"])
    totals_final = json.loads(status_final.output)["data"]["totals"]["by_status"]
    assert totals_final["indexed"] == 1
    assert totals_final.get("failed", 0) == 0


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks need elevated privileges on Windows"
)
def test_symlink_escape_is_skipped_without_crashing(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("print('a')")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    (source_dir / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, source_dir)
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0

    status = runner.invoke(app, ["status", "--json"])
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    assert totals.get("indexed", 0) == 1
