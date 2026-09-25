"""Indexing optimization plan, Phase P7: regression test for a real,
pre-existing bug the plan's end-to-end regression test caught -- an
existing file transitioning from UNCHANGED to CHANGED had its new
on-disk size/mtime/content_hash computed during scanning, but never
actually persisted to its ``files`` row (only ``status`` was updated).
``_process_queue`` then re-fetched the *stale* row and ``mark_indexed``
dutifully wrote those stale values straight back -- so the file looked
CHANGED again on every subsequent run, forever, regardless of Phase
P3's content-hash reuse or Phase P2's targeted scanning (both correctly
detected a real mismatch against genuinely stale stored data; neither
was the bug).

Covers both the full-scan path (``IndexCoordinator.run()``) and the
targeted path (``run(changed_paths=...)``, Phase P2).
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.config import RagMonkConfig
from ragmonk.indexing.coordinator import IndexCoordinator
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect


def test_a_second_full_scan_after_an_edit_reports_no_further_change(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("def a():\n    return 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())
        first = coord.run()
        assert first.new == 1

        target.write_text("def a():\n    return 2, 'more content to change the size'\n")
        second = coord.run()
        assert second.changed == 1

        record = files_repo.get_by_path(conn, "s1", str(target))
        assert record is not None
        assert record.size == target.stat().st_size
        assert record.content_hash is not None

        # The regression: without the fix, this third run still sees
        # the file as CHANGED because its stored stat never actually
        # updated on the second run.
        third = coord.run()
        assert third.changed == 0
        assert third.unchanged == 1
    finally:
        conn.close()


def test_a_second_targeted_scan_after_an_edit_reports_no_further_change(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("def a():\n    return 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())
        first = coord.run()
        assert first.new == 1

        target.write_text("def a():\n    return 2, 'more content to change the size'\n")
        target_path = str(target)
        second = coord.run(changed_paths=frozenset({target_path}))
        assert second.changed == 1

        record = files_repo.get_by_path(conn, "s1", target_path)
        assert record is not None
        assert record.size == target.stat().st_size

        third = coord.run(changed_paths=frozenset({target_path}))
        assert third.changed == 0
        assert third.unchanged == 1
    finally:
        conn.close()
