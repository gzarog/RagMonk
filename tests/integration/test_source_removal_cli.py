"""Phase 1A: ``ragpilot source remove`` must be a confirmed, all-or-nothing
operation -- the entire derived project directory (``knowledge.db``,
vectors, cache, state) gone, the registry row gone, the original source
files on disk untouched, and a since-removed source's data never
resurfacing in search -- or, on a declined prompt or a filesystem error,
nothing touched at all. See ``sources/registry.py``'s ``SourceRegistry.
remove`` and ``cli/source.py``'s ``remove`` for the implementation this
exercises through the real CLI.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.core import paths
from ragpilot.service import pid
from ragpilot.sources import registry as registry_module
from ragpilot.storage.repositories import entities_repo
from ragpilot.storage.sqlite import connect

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _write_marker_class(root: Path, class_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "service.py").write_text(f"class {class_name}:\n    def run(self):\n        pass\n")


def _entity_titles(runner: CliRunner, query: str) -> list[str]:
    result = runner.invoke(app, ["search", query, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    return [r["title"] for r in payload["results"] if r["kind"] == "entity"]


def _has_entity(runner: CliRunner, class_name: str) -> bool:
    return any(class_name in title for title in _entity_titles(runner, class_name))


def _list_ids(runner: CliRunner) -> str:
    result = runner.invoke(app, ["source", "list"])
    assert result.exit_code == 0, result.output
    return result.output


def _project_dir_for(home: Path, source_dir: Path) -> Path:
    project_id = paths.project_id_for_path(source_dir)
    return paths.project_dir(project_id, home)


def _entity_count_on_disk(home: Path, source_dir: Path) -> int:
    """Reads the entity count directly from the project's on-disk
    ``knowledge.db``, bypassing ``ragpilot search`` and its process-local
    result cache entirely -- unlike a real deployment (a separate OS
    process per CLI invocation), a same-process ``CliRunner`` sequence
    can otherwise observe a stale cache entry keyed off a data-version
    baseline that resets once every connection to a brand-new database
    closes (see ``retrieval/cache.py``'s module docstring); reading the
    row count straight from the file sidesteps that entirely.
    """
    project_id = paths.project_id_for_path(source_dir)
    db_path = paths.project_db_path(project_id, home)
    if not db_path.is_file():
        return 0
    conn = connect(db_path)
    try:
        return entities_repo.count_all(conn)
    finally:
        conn.close()


def _setup_indexed_source(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, class_name: str
) -> tuple[Path, str]:
    source_dir = tmp_path / "src"
    _write_marker_class(source_dir, class_name)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, source_dir)
    assert runner.invoke(app, ["index"]).exit_code == 0
    return source_dir, source_id


def test_declining_confirmation_removes_nothing(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "DeclineMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)
    assert project_dir.is_dir()

    result = runner.invoke(app, ["source", "remove", source_id], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert source_id in _list_ids(runner)
    assert project_dir.is_dir()
    assert (source_dir / "service.py").is_file()


def test_eof_on_prompt_removes_nothing(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "EofMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)

    # No input at all -- CliRunner's stdin hits EOF immediately, the same
    # as a real Ctrl+D at the prompt.
    result = runner.invoke(app, ["source", "remove", source_id], input="")

    assert result.exit_code != 0
    assert source_id in _list_ids(runner)
    assert project_dir.is_dir()
    assert (source_dir / "service.py").is_file()


def test_empty_line_on_prompt_removes_nothing(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "EmptyLineMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)

    # A bare Enter at the prompt: an explicit (if empty) answer, not EOF --
    # confirm's own default (no) applies, same outcome either way.
    result = runner.invoke(app, ["source", "remove", source_id], input="\n")

    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert source_id in _list_ids(runner)
    assert project_dir.is_dir()
    assert (source_dir / "service.py").is_file()


def test_yes_flag_skips_prompt_and_removes(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "YesFlagMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)

    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])

    assert result.exit_code == 0, result.output
    assert "Proceed?" not in result.output
    assert source_id not in _list_ids(runner)
    assert not project_dir.exists()
    # The original source files are never touched, only derived data.
    assert (source_dir / "service.py").is_file()


def test_removal_deletes_registry_row_and_full_project_dir(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "FullWipeMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)
    assert (project_dir / "knowledge.db").is_file()

    # An arbitrary extra artifact under the project dir -- standing in for
    # any derived file a later phase adds (vectors, caches, ...): the
    # point of deleting the directory wholesale is that this is removed
    # too, with nothing to enumerate by name.
    (project_dir / "state" / "extra_marker.json").write_text("{}")

    info_before = runner.invoke(app, ["source", "info", source_id])
    assert info_before.exit_code == 0

    result = runner.invoke(app, ["source", "remove", source_id], input="y\n")
    assert result.exit_code == 0, result.output

    assert not project_dir.exists()
    info_after = runner.invoke(app, ["source", "info", source_id])
    assert info_after.exit_code != 0
    assert source_id not in _list_ids(runner)
    assert (source_dir / "service.py").is_file()


def test_missing_project_dir_still_allows_registry_removal(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "NoDirMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)
    assert project_dir.is_dir()

    import shutil

    shutil.rmtree(project_dir)
    assert not project_dir.exists()

    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])

    assert result.exit_code == 0, result.output
    assert source_id not in _list_ids(runner)


def test_filesystem_deletion_failure_keeps_source_registered(
    ragpilot_home: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "FailureMarker")
    project_dir = _project_dir_for(ragpilot_home, source_dir)
    assert project_dir.is_dir()

    def _raise_rmtree(path: object) -> None:
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(registry_module.shutil, "rmtree", _raise_rmtree)

    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])

    assert result.exit_code != 0
    # Rich's console wraps long error lines at the detected terminal width,
    # which is narrower on Windows CI runners than elsewhere -- collapse
    # whitespace/newlines before matching so this doesn't depend on where
    # that wrap happens to fall.
    assert "simulated filesystem failure" in " ".join(result.output.split())
    # Not partially removed: the row is still there, and untouched.
    assert source_id in _list_ids(runner)
    info = runner.invoke(app, ["source", "info", source_id])
    assert info.exit_code == 0
    assert json.loads(info.stdout)["id"] == source_id


def test_removed_source_not_in_list(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "ListMarker")
    assert source_id in _list_ids(runner)

    assert runner.invoke(app, ["source", "remove", source_id, "--yes"]).exit_code == 0

    assert source_id not in _list_ids(runner)


def test_search_returns_nothing_from_a_removed_source(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "SearchGoneMarker")
    assert _has_entity(runner, "SearchGoneMarker")

    assert runner.invoke(app, ["source", "remove", source_id, "--yes"]).exit_code == 0

    assert not _has_entity(runner, "SearchGoneMarker")


def test_readding_same_path_after_removal_is_clean_no_stale_data(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this whole phase exists to fix: before, ``remove``
    only deleted the ``sources`` row -- the project directory (and its
    already-indexed generation of data) was left behind on disk, keyed by
    a project id that is a deterministic hash of the canonical source
    path. Re-adding the exact same path reused that same, stale
    ``knowledge.db`` rather than a fresh one, so the old content could
    resurface in search results before ``ragpilot index`` was even run
    again.
    """
    source_dir, source_id = _setup_indexed_source(runner, tmp_path, monkeypatch, "StaleMarkerZzz")
    assert _has_entity(runner, "StaleMarkerZzz")

    assert runner.invoke(app, ["source", "remove", source_id], input="y\n").exit_code == 0

    # Re-add the identical path -- same canonical path, so the same
    # deterministic source/project id -- before running `index` again.
    re_added_id = _add_source(runner, source_dir)
    assert re_added_id == source_id

    # If the old project directory had survived the removal, this would
    # already show the stale entity even though nothing has been indexed
    # in this new generation yet.
    assert _entity_count_on_disk(ragpilot_home, source_dir) == 0

    # A fresh index against the (unchanged) source files finds it again,
    # from a clean generation.
    assert runner.invoke(app, ["index"]).exit_code == 0
    assert _has_entity(runner, "StaleMarkerZzz")


def test_remove_refuses_while_a_daemon_is_running(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir, source_id = _setup_indexed_source(
        runner, tmp_path, monkeypatch, "DaemonGuardMarker"
    )
    project_dir = _project_dir_for(ragpilot_home, source_dir)

    # Simulate a live daemon the same way tests/unit/test_service_pid.py
    # does: a PID file pointing at this very (definitely alive) test
    # process, without spawning a real background process.
    pid.write_pid_file(ragpilot_home, os.getpid())

    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])

    assert result.exit_code != 0
    normalized_output = " ".join(result.output.split())
    assert "daemon" in normalized_output.lower()
    assert "ragpilot daemon stop" in normalized_output
    assert source_id in _list_ids(runner)
    assert project_dir.is_dir()
