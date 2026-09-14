"""``IndexCoordinator.run()``'s own defense against a duplicate scanned
path landing on ``files``' ``UNIQUE(source_id, path)`` constraint --
independent of and complementary to ``scan()``'s own intra-run dedup
(see ``test_scanner_dedup.py``): ``existing_by_path`` used to be a
snapshot taken once before the per-file loop, so a duplicate scanned
entry (whatever produced it) was misclassified as "new" a second time
and crashed the whole run instead of being recognized as unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragpilot.core.config import RagpilotConfig
from ragpilot.core.models import ScannedFile
from ragpilot.indexing import coordinator as coordinator_module
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import files_repo
from ragpilot.storage.sqlite import connect


def test_a_duplicate_scanned_path_does_not_crash_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    real_file = root / "a.txt"
    real_file.write_text("hello")
    stat = real_file.stat()

    # scan() already prevents this on its own (test_scanner_dedup.py) --
    # this bypasses it entirely to prove the coordinator's own loop is
    # independently safe against a duplicate, from whatever source.
    duplicate_path = str(real_file.resolve())
    scanned_twice = [
        ScannedFile(path=duplicate_path, size=stat.st_size, mtime=stat.st_mtime),
        ScannedFile(path=duplicate_path, size=stat.st_size, mtime=stat.st_mtime),
    ]
    monkeypatch.setattr(coordinator_module, "scan", lambda *args, **kwargs: iter(scanned_twice))

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = coordinator_module.IndexCoordinator(
            conn,
            "s1",
            str(root),
            [],
            [],
            RagpilotConfig(),
        )

        result = coord.run()

        assert result.scanned == 2
        assert result.new == 1
        assert result.unchanged == 1
        assert len(files_repo.list_by_source(conn, "s1")) == 1
    finally:
        conn.close()
