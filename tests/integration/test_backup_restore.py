"""Backup/restore round-trip, corrupted-archive abort safety, and proof
that backup uses SQLite's online backup API rather than a raw file copy
(see ``ops/backup.py``/``ops/restore.py``).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.errors import EXIT_DATABASE_ERROR


def _write_project(tmp_path: Path) -> Path:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def foo():\n    return bar()\n\ndef bar():\n    return 1\n")
    (source_dir / "readme.md").write_text("# Title\n\nDocs about foo().\n")
    return source_dir


def test_backup_restore_round_trip_preserves_state(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    status_before = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    symbol_before = json.loads(runner.invoke(app, ["symbol", "foo", "--json"]).output)["data"]
    assert status_before["totals"]["metrics"]["symbols_created"] > 0

    backup_result = runner.invoke(app, ["backup", "--json"])
    assert backup_result.exit_code == 0, backup_result.output
    archive = json.loads(backup_result.output)["data"]["archive"]

    # Simulate the DB being irrecoverably lost -- source files (source_dir)
    # are left completely untouched, matching the blueprint's "source
    # files = truth" principle this whole command exists to make good on.
    shutil.rmtree(paths.projects_dir(ragmonk_home))
    for suffix in ("", "-wal", "-shm"):
        p = paths.sources_db_path(ragmonk_home).with_name(f"sources.db{suffix}")
        p.unlink(missing_ok=True)

    empty_status = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    assert empty_status["sources"] == []

    restore_result = runner.invoke(app, ["restore", archive, "--json"])
    assert restore_result.exit_code == 0, restore_result.output
    restore_payload = json.loads(restore_result.output)["data"]
    assert restore_payload["daemon_was_running"] is False
    assert len(restore_payload["projects_restored"]) == 1

    status_after = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    symbol_after = json.loads(runner.invoke(app, ["symbol", "foo", "--json"]).output)["data"]

    assert status_after["totals"]["by_status"] == status_before["totals"]["by_status"]
    assert status_after["totals"]["metrics"] == status_before["totals"]["metrics"]
    assert symbol_after == symbol_before


def test_restore_aborts_cleanly_on_corrupted_archive(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    status_before = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]

    staging = tmp_path / "corrupt_staging"
    staging.mkdir()
    (staging / "sources.db").write_bytes(b"this is not a valid sqlite file")
    (staging / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "ragmonk_version": "0.1.0",
                "created_at": "2026-01-01T00:00:00+00:00",
                "sources_schema_version": 2,
                "projects": {},
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    corrupt_archive = tmp_path / "corrupt.tar.gz"
    with tarfile.open(corrupt_archive, "w:gz") as tar:
        tar.add(staging, arcname=".")

    result = runner.invoke(app, ["restore", str(corrupt_archive), "--json"])
    assert result.exit_code == EXIT_DATABASE_ERROR, result.output

    # The pre-restore state must be completely untouched by the aborted
    # attempt -- never "worse off than before it was attempted".
    status_after = json.loads(runner.invoke(app, ["status", "--json"]).output)["data"]
    assert status_after == status_before


def test_restore_refuses_archive_missing_manifest(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    staging = tmp_path / "no_manifest"
    staging.mkdir()
    (staging / "sources.db").write_bytes(b"irrelevant")
    archive = tmp_path / "no_manifest.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=".")

    result = runner.invoke(app, ["restore", str(archive), "--json"])
    assert result.exit_code == EXIT_DATABASE_ERROR, result.output


def test_backup_captures_wal_buffered_writes_not_yet_checkpointed(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    """Proves ``ops/backup.py`` exercises SQLite's online backup API, not a
    raw file copy: a write committed only to the WAL file (never
    checkpointed into the main ``.db`` file) is still present in the
    backup, even though a naive copy of just the main file would miss it.
    """
    assert runner.invoke(app, ["init"]).exit_code == 0

    sources_db_path = paths.sources_db_path(ragmonk_home)
    marker_conn = sqlite3.connect(str(sources_db_path), isolation_level=None)
    try:
        marker_conn.execute("PRAGMA journal_mode = WAL")
        marker_conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)", ("wal_test_marker", "present")
        )

        naive_copy = tmp_path / "naive_copy.db"
        shutil.copy2(sources_db_path, naive_copy)
        naive_conn = sqlite3.connect(str(naive_copy))
        naive_row = naive_conn.execute(
            "SELECT value FROM metadata WHERE key = ?", ("wal_test_marker",)
        ).fetchone()
        naive_conn.close()
        assert naive_row is None, (
            "test setup assumption broken: the WAL write was already checkpointed into "
            "the main file, so this run cannot distinguish an online backup from a raw copy"
        )

        result = runner.invoke(app, ["backup", "--json"])
        assert result.exit_code == 0, result.output
        archive_path = Path(json.loads(result.output)["data"]["archive"])
    finally:
        marker_conn.close()

    staged = tmp_path / "extracted"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(staged, filter="data")

    backup_conn = sqlite3.connect(str(staged / "sources.db"))
    try:
        row = backup_conn.execute(
            "SELECT value FROM metadata WHERE key = ?", ("wal_test_marker",)
        ).fetchone()
    finally:
        backup_conn.close()
    assert row is not None
    assert row[0] == "present"
