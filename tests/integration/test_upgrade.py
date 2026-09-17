"""``ragmonk upgrade``: no-op when already current, backup-first when a
migration is pending, and reuse of ``ragmonk doctor``'s health check.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.storage import migrations as migrations_module
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect


def test_upgrade_with_nothing_pending_is_a_safe_noop(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    before_backups = list(paths.backups_dir(ragmonk_home).glob("*.tar.gz"))

    result = runner.invoke(app, ["upgrade", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]

    assert payload["pending"] is False
    assert payload["backup_archive"] is None
    assert payload["healthy_after"] is True
    for db in payload["databases"]:
        assert db["before"] == db["after"]

    after_backups = list(paths.backups_dir(ragmonk_home).glob("*.tar.gz"))
    assert after_backups == before_backups


def test_upgrade_backs_up_before_applying_a_pending_migration(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0

    # Simulate a runtime directory stuck on an old schema version: rebuild
    # sources.db with only the *first* of its two migrations applied, by
    # temporarily truncating the registered migration list to match the
    # established pattern for exercising "a migration is pending"
    # (see tests/unit/test_migrations.py for the equivalent non-Phase-8 use
    # of this same MIGRATIONS structure).
    sources_db_path = paths.sources_db_path(ragmonk_home)
    sources_db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sources_db_path.with_name(f"sources.db{suffix}").unlink(missing_ok=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            migrations_module.MIGRATIONS, "sources", migrations_module.MIGRATIONS["sources"][:1]
        )
        conn = connect(sources_db_path)
        apply_migrations(conn, "sources")
        conn.close()

    stale_conn = connect(sources_db_path)
    try:
        assert migrations_module.current_version(stale_conn) == 1
    finally:
        stale_conn.close()

    result = runner.invoke(app, ["upgrade", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]

    assert payload["pending"] is True
    assert payload["backup_archive"] is not None
    archive_path = Path(payload["backup_archive"])
    assert archive_path.is_file()
    with tarfile.open(archive_path, "r:gz") as tar:
        assert "./sources.db" in tar.getnames()

    sources_db_entry = next(db for db in payload["databases"] if db["name"] == "sources")
    assert sources_db_entry["before"] == 1
    assert sources_db_entry["after"] == 2
    assert payload["healthy_after"] is True

    # The backup was taken *before* the migration ran: its own copy of
    # sources.db must still show the pre-upgrade version.
    with tarfile.open(archive_path, "r:gz") as tar:
        staged = tmp_path / "extracted"
        tar.extractall(staged, filter="data")
    backup_conn = connect(staged / "sources.db")
    try:
        assert migrations_module.current_version(backup_conn) == 1
    finally:
        backup_conn.close()

    # And the live sources.db really is fully upgraded now.
    live_conn = connect(sources_db_path)
    try:
        assert migrations_module.current_version(live_conn) == 2
    finally:
        live_conn.close()
