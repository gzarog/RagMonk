"""``IndexCoordinator.run(changed_paths=...)`` -- the targeted-pass path
introduced by the indexing optimization plan's Phase P2 (findings F1/
F2): examines exactly the given paths instead of walking the whole
source tree, while staying precise about new/changed/deleted files and
preserving rename identity for the common same-batch case.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.config import RagMonkConfig
from ragmonk.indexing.coordinator import IndexCoordinator, ScanRequest
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect


def _coordinator(root: Path, conn) -> IndexCoordinator:  # noqa: ANN001
    return IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())


def test_targeted_run_indexes_only_the_named_new_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    watched = root / "watched.py"
    watched.write_text("x = 1\n")
    unwatched = root / "unwatched.py"
    unwatched.write_text("y = 2\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)

        result = coord.run(changed_paths=frozenset({str(watched.resolve())}))

        assert result.targeted is True
        assert result.new == 1
        assert result.scanned == 1
        records = files_repo.list_by_source(conn, "s1")
        assert len(records) == 1
        assert records[0].path == str(watched.resolve())
        # The unwatched file was never named, so a targeted pass never
        # sees it at all -- exactly the point of not walking the tree.
    finally:
        conn.close()


def test_targeted_run_deletes_only_the_named_missing_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    kept = root / "kept.py"
    kept.write_text("x = 1\n")
    gone = root / "gone.py"
    gone.write_text("y = 2\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        full = coord.run()
        assert full.new == 2

        gone.unlink()
        result = coord.run(changed_paths=frozenset({str(gone.resolve())}))

        assert result.targeted is True
        assert result.deleted == 1
        records = files_repo.list_by_source(conn, "s1")
        assert len(records) == 1
        assert records[0].path == str(kept.resolve())
    finally:
        conn.close()


def test_targeted_run_reprocesses_a_changed_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("x = 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        coord.run()

        target.write_text("x = 2\n")
        result = coord.run(changed_paths=frozenset({str(target.resolve())}))

        assert result.targeted is True
        assert result.changed == 1
        assert result.indexed == 1
    finally:
        conn.close()


def test_targeted_run_of_an_unmodified_file_is_a_noop(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("x = 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        coord.run()

        # A spurious/duplicate watcher trigger for a path that didn't
        # actually change (e.g. an editor's atomic-write-no-op-save).
        result = coord.run(changed_paths=frozenset({str(target.resolve())}))

        assert result.unchanged == 1
        assert result.changed == 0
        assert result.new == 0
    finally:
        conn.close()


def test_targeted_run_preserves_rename_identity_in_the_same_batch(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    original = root / "a.py"
    original.write_text("identical content\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        coord.run()
        [before] = files_repo.list_by_source(conn, "s1")

        target = root / "b.py"
        original.rename(target)

        # Both halves of the rename land in the same batch -- exactly
        # what the watcher's own debouncing delivers for a local
        # editor/`git mv` in the common case.
        result = coord.run(
            changed_paths=frozenset({str(original.resolve()), str(target.resolve())})
        )

        assert result.targeted is True
        assert result.moved == 1
        assert result.new == 0
        assert result.deleted == 0
        [after] = files_repo.list_by_source(conn, "s1")
        assert after.id == before.id  # same row, derived content preserved
        assert after.path == str(target.resolve())
    finally:
        conn.close()


def test_targeted_run_without_the_renamed_pair_falls_back_to_delete_and_recreate(
    tmp_path: Path,
) -> None:
    """A rename split across two separate targeted batches (the old
    path's disappearance processed alone, with the new path's arrival
    only surfacing in a later batch) cannot preserve identity -- there
    is nothing in the current batch to match it against. This is the
    disclosed, accepted degradation for that case (see
    ``IndexCoordinator._run_targeted``'s docstring).
    """
    root = tmp_path / "source"
    root.mkdir()
    original = root / "a.py"
    original.write_text("identical content\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        coord.run()
        [before] = files_repo.list_by_source(conn, "s1")

        target = root / "b.py"
        original.rename(target)

        only_old_path = coord.run(changed_paths=frozenset({str(original.resolve())}))
        assert only_old_path.deleted == 1
        assert only_old_path.moved == 0

        only_new_path = coord.run(changed_paths=frozenset({str(target.resolve())}))
        assert only_new_path.new == 1
        assert only_new_path.moved == 0

        [after] = files_repo.list_by_source(conn, "s1")
        assert after.id != before.id
        assert after.path == str(target.resolve())
    finally:
        conn.close()


def test_targeted_run_of_offline_source_reports_offline_not_mass_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("x = 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = _coordinator(root, conn)
        coord.run()

        missing_root = tmp_path / "does-not-exist-anymore"
        offline_coord = IndexCoordinator(conn, "s1", str(missing_root), [], [], RagMonkConfig())
        result = offline_coord.run(changed_paths=frozenset({str(target.resolve())}))

        assert result.source_offline is True
        assert result.deleted == 0
        assert len(files_repo.list_by_source(conn, "s1")) == 1
    finally:
        conn.close()


def test_scan_request_rejects_non_full_with_no_changed_paths() -> None:
    import pytest

    with pytest.raises(ValueError, match="changed path"):
        ScanRequest(source_id="s1", full=False)
