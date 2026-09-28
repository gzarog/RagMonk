"""Last Scan bookkeeping and per-source failure isolation: a post-scan
stage failure must keep ``last_scan_at`` (plus a useful ``last_error``),
and one failing source must not stop ``ragmonk index`` from indexing the
remaining sources -- while the command still exits non-zero.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.errors import EXIT_INDEXING_PARTIAL_FAILURE
from ragmonk.indexing import runner as runner_mod
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _info(runner: CliRunner, source_id: str) -> dict:
    result = runner.invoke(app, ["source", "info", source_id])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _file_count(home: Path, source_path: Path, source_id: str) -> int:
    conn = connect(paths.project_db_path(paths.project_id_for_path(source_path), home))
    try:
        return len(files_repo.list_by_source(conn, source_id))
    finally:
        conn.close()


def _make_source(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "a.py").write_text("def a():\n    return 1\n")
    (d / "b.py").write_text("def b():\n    return a()\n")
    return d


def test_post_scan_failure_keeps_last_scan_and_records_error(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = _make_source(tmp_path, "src")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("linker exploded")

    with monkeypatch.context() as m:
        m.setattr(runner_mod, "link_touched_files", boom)
        result = runner.invoke(app, ["index"])
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE, result.output
    assert "linker exploded" in result.output

    info = _info(runner, source_id)
    assert _file_count(ragmonk_home, source_dir, source_id) == 2
    assert info["last_scan_at"] is not None
    assert info["last_error"] == "linking failed: linker exploded"
    assert info["status"] == "active"
    first_scan = info["last_scan_at"]

    # T4: a later successful pass clears the transient error and
    # advances Last Scan.
    (source_dir / "c.py").write_text("def c():\n    return 3\n")
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output
    info = _info(runner, source_id)
    assert info["last_error"] is None
    assert info["status"] == "active"
    assert info["last_scan_at"] > first_scan


def test_run_source_pass_reraises_original_failure(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    from ragmonk.core.lifecycle import AppContext
    from ragmonk.sources.registry import SourceRegistry

    source_dir = _make_source(tmp_path, "src")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)

    class Boom(Exception):
        pass

    def boom(*args, **kwargs):
        raise Boom("nope")

    monkeypatch.setattr(runner_mod, "link_touched_files", boom)
    with AppContext.bootstrap() as ctx:
        source = SourceRegistry(ctx.sources_conn, home=ctx.home).get(source_id)
        processors = runner_mod.build_processor_registry(ctx.config)
        try:
            runner_mod.run_source_pass(ctx, source, processors)
        except Boom:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected the original exception")
    assert _info(runner, source_id)["last_scan_at"] is not None


def test_index_continues_after_one_source_fails(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    dirs = [_make_source(tmp_path, f"src{i}") for i in range(3)]
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    ids = [_add_source(runner, d) for d in dirs]

    real = runner_mod.run_source_pass
    attempted: list[str] = []

    def flaky(ctx, source, processors, **kwargs):
        attempted.append(source.id)
        if source.id == ids[1]:
            raise RuntimeError("source two broke")
        return real(ctx, source, processors, **kwargs)

    import ragmonk.cli.index as index_cli

    monkeypatch.setattr(index_cli, "run_source_pass", flaky)
    result = runner.invoke(app, ["index"])
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE, result.output
    assert sorted(attempted) == sorted(ids)
    assert "source two broke" in result.output
    assert ids[1] in result.output
    assert "1 source(s) failed" in result.output
    for i in (0, 2):
        assert _file_count(ragmonk_home, dirs[i], ids[i]) == 2
        assert _info(runner, ids[i])["last_scan_at"] is not None


def test_offline_source_bookkeeping_unchanged(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = _make_source(tmp_path, "src")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0

    shutil.move(str(source_dir), str(tmp_path / "gone"))
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output
    info = _info(runner, source_id)
    assert info["status"] == "offline"
    assert info["last_scan_at"] is not None
    assert info["last_error"]
    assert _file_count(ragmonk_home, source_dir, source_id) == 2
