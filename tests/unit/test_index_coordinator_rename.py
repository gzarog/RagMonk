"""Search Quality Improvement Plan, Phase 12: ``IndexCoordinator.run()``
detects a same-source path change (move/rename) by content hash and
reassigns the existing file row in place, rather than the delete-then-
insert-as-new a plain path mismatch would otherwise cause -- see
``indexing/coordinator.py``'s ``_reconcile_renames``.
"""

from __future__ import annotations

from pathlib import Path

from ragpilot.core.config import RagpilotConfig
from ragpilot.indexing.coordinator import IndexCoordinator
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import files_repo
from ragpilot.storage.sqlite import connect


def _run(root: Path, conn) -> IndexCoordinator:  # noqa: ANN001
    return IndexCoordinator(conn, "s1", str(root), [], [], RagpilotConfig())


def test_a_moved_file_reuses_its_existing_row_instead_of_delete_and_recreate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    original = root / "a.txt"
    original.write_text("identical content")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        first = _run(root, conn).run()
        assert first.new == 1
        assert first.moved == 0
        [before] = files_repo.list_by_source(conn, "s1")
        assert before.path == str(original)

        moved_path = root / "subdir"
        moved_path.mkdir()
        target = moved_path / "b.txt"
        original.rename(target)

        second = _run(root, conn).run()
        assert second.moved == 1
        assert second.new == 0
        assert second.deleted == 0
        assert second.changed == 0

        [after] = files_repo.list_by_source(conn, "s1")
        assert after.id == before.id
        assert after.path == str(target)
        assert after.content_hash == before.content_hash
        assert after.generation == before.generation
    finally:
        conn.close()


def test_a_touched_but_still_processed_file_is_not_treated_as_a_move(
    tmp_path: Path,
) -> None:
    """A path that disappears because its content genuinely changed (a
    brand-new file at a brand-new path, unrelated content) must still go
    through ordinary NEW/DELETED handling -- rename detection only fires
    on an exact, unambiguous content-hash match.
    """
    root = tmp_path / "source"
    root.mkdir()
    original = root / "a.txt"
    original.write_text("content A")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        _run(root, conn).run()

        original.unlink()
        (root / "b.txt").write_text("totally different content B")

        result = _run(root, conn).run()
        assert result.moved == 0
        assert result.new == 1
        assert result.deleted == 1

        records = files_repo.list_by_source(conn, "s1")
        assert len(records) == 1
        assert records[0].path == str(root / "b.txt")
    finally:
        conn.close()


def test_an_ambiguous_hash_match_falls_back_to_delete_and_recreate(tmp_path: Path) -> None:
    """Two files disappear with the same content hash and one new path
    appears with that hash -- which old file "moved" is genuinely
    ambiguous, so this is left to ordinary delete+recreate rather than
    guessed at.
    """
    root = tmp_path / "source"
    root.mkdir()
    dup_a = root / "a.txt"
    dup_b = root / "b.txt"
    dup_a.write_text("same content")
    dup_b.write_text("same content")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        first = _run(root, conn).run()
        assert first.new == 2

        dup_a.unlink()
        dup_b.unlink()
        (root / "c.txt").write_text("same content")

        result = _run(root, conn).run()
        assert result.moved == 0
        assert result.new == 1
        assert result.deleted == 2
    finally:
        conn.close()
