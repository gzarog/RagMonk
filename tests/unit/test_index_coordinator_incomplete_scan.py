"""``IndexCoordinator.run()`` must never treat an incomplete scan (see
``sources/scanner.py``'s ``ScanOutcome``) as proof that a missing file
was deleted -- indexing optimization plan finding F6. New/changed files
found in whatever *was* successfully scanned are still processed; only
"missing means deleted" is unsafe on a partial scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.config import RagMonkConfig
from ragmonk.core.models import FileStatus, ScannedFile
from ragmonk.indexing import coordinator as coordinator_module
from ragmonk.sources.scanner import ScanError
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect


def _patch_scan(
    monkeypatch: pytest.MonkeyPatch, scanned: list[ScannedFile], errors: list[ScanError]
) -> None:
    def _fake_scan(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        outcome = kwargs.get("outcome")
        if outcome is not None:
            outcome.errors.extend(errors)
        return iter(scanned)

    monkeypatch.setattr(coordinator_module, "scan", _fake_scan)


def test_incomplete_scan_skips_deletion_but_still_processes_present_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    kept = root / "kept.py"
    kept.write_text("x = 1\n")
    stat = kept.stat()

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = coordinator_module.IndexCoordinator(
            conn, "s1", str(root), [], [], RagMonkConfig()
        )

        # First pass: a complete, normal scan indexes both files.
        missing_later = root / "missing_later.py"
        missing_later.write_text("y = 2\n")
        missing_stat = missing_later.stat()
        _patch_scan(
            monkeypatch,
            [
                ScannedFile(path=str(kept.resolve()), size=stat.st_size, mtime=stat.st_mtime),
                ScannedFile(
                    path=str(missing_later.resolve()),
                    size=missing_stat.st_size,
                    mtime=missing_stat.st_mtime,
                ),
            ],
            [],
        )
        first = coord.run()
        assert not first.scan_incomplete
        assert len(files_repo.list_by_source(conn, "s1")) == 2

        # Second pass: simulate an incomplete scan where
        # missing_later.py's subtree couldn't be listed (scan() only
        # yields kept.py) *and* scan() reports an error for it.
        _patch_scan(
            monkeypatch,
            [ScannedFile(path=str(kept.resolve()), size=stat.st_size, mtime=stat.st_mtime)],
            [ScanError(path=str(root / "somewhere"), message="permission denied")],
        )
        second = coord.run()

        assert second.scan_incomplete
        assert second.scan_errors
        assert second.deleted == 0
        records = files_repo.list_by_source(conn, "s1")
        assert len(records) == 2
        assert {r.path for r in records} == {str(kept.resolve()), str(missing_later.resolve())}
    finally:
        conn.close()


def test_complete_scan_still_deletes_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    kept = root / "kept.py"
    kept.write_text("x = 1\n")
    stat = kept.stat()

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = coordinator_module.IndexCoordinator(
            conn, "s1", str(root), [], [], RagMonkConfig()
        )

        gone = root / "gone.py"
        gone.write_text("y = 2\n")
        gone_stat = gone.stat()
        kept_sf = ScannedFile(path=str(kept.resolve()), size=stat.st_size, mtime=stat.st_mtime)
        gone_sf = ScannedFile(
            path=str(gone.resolve()), size=gone_stat.st_size, mtime=gone_stat.st_mtime
        )
        _patch_scan(monkeypatch, [kept_sf, gone_sf], [])
        coord.run()
        assert len(files_repo.list_by_source(conn, "s1")) == 2

        # A genuinely complete scan (no errors) that no longer sees
        # gone.py must still delete it -- P1 only suppresses deletion on
        # an *incomplete* scan, real deletions must keep working.
        _patch_scan(monkeypatch, [kept_sf], [])
        result = coord.run()

        assert not result.scan_incomplete
        assert result.deleted == 1
        records = files_repo.list_by_source(conn, "s1")
        assert len(records) == 1
        assert records[0].status is FileStatus.INDEXED
    finally:
        conn.close()
