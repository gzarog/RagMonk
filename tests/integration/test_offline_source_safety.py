"""Phase 7's central safety property: a source root that becomes
temporarily inaccessible (an unmounted network drive, a permissions
error, a deleted directory) must never be misread as "every file in it
was deleted". ``ragmonk index`` alone -- no watcher/daemon running --
must flip the source OFFLINE and leave every existing entity/file
untouched, then flip back to ACTIVE and reconcile for real once the root
is reachable again.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _file_count(home: Path, source_path: Path, source_id: str) -> int:
    project_id = paths.project_id_for_path(source_path)
    conn = connect(paths.project_db_path(project_id, home))
    try:
        return len(files_repo.list_by_source(conn, source_id))
    finally:
        conn.close()


def _source_status(runner: CliRunner, source_id: str) -> str:
    # ``source info`` prints the bare Source model via Rich's
    # console.print_json (cli/source.py), not the schema_version/data
    # envelope most other `--json` commands use.
    result = runner.invoke(app, ["source", "info", source_id])
    assert result.exit_code == 0, result.output
    return str(json.loads(result.stdout)["status"])


def test_index_never_deletes_knowledge_when_source_root_goes_offline(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def a():\n    return 1\n")
    (source_dir / "b.py").write_text("def b():\n    return 2\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)

    assert runner.invoke(app, ["index"]).exit_code == 0
    assert _source_status(runner, source_id) == "active"
    files_before = _file_count(ragmonk_home, source_dir, source_id)
    assert files_before == 2

    # Simulate the root going offline (an unmounted network share, a
    # deleted directory): move it out from under the registered path.
    elsewhere = tmp_path / "src-moved-away"
    shutil.move(str(source_dir), str(elsewhere))
    assert not source_dir.exists()

    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output

    assert _source_status(runner, source_id) == "offline"
    # The critical assertion: nothing was deleted just because the root
    # could not be listed this run.
    assert _file_count(ragmonk_home, source_dir, source_id) == files_before

    info = runner.invoke(app, ["source", "info", source_id])
    payload = json.loads(info.stdout)
    assert payload["last_error"]

    doctor = runner.invoke(app, ["doctor", "--json"])
    assert doctor.exit_code == 0
    doctor_payload = json.loads(doctor.stdout)["data"]
    assert doctor_payload["result"] == "HEALTHY WITH WARNINGS"

    # Restore the root -- back to ACTIVE, and this run's diff is
    # trustworthy again: a real deletion (a.py) and a real addition
    # (c.py) both get reconciled.
    shutil.move(str(elsewhere), str(source_dir))
    (source_dir / "a.py").unlink()
    (source_dir / "c.py").write_text("def c():\n    return 3\n")

    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output
    assert _source_status(runner, source_id) == "active"
    assert _file_count(ragmonk_home, source_dir, source_id) == 2


def test_status_is_offline_only_while_root_is_unreachable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert _source_status(runner, source_id) == "active"

    shutil.rmtree(source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert _source_status(runner, source_id) == "offline"

    source_dir.mkdir()
    (source_dir / "a.txt").write_text("hello again")
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert _source_status(runner, source_id) == "active"


def test_source_list_and_doctor_surface_offline_status(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0

    shutil.rmtree(source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0

    listing = runner.invoke(app, ["source", "list"])
    assert listing.exit_code == 0
    assert "offline" in listing.output

    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0
    assert "WARN" in doctor.output
