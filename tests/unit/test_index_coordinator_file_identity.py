"""``IndexCoordinator`` threads a verified ``FileIdentity`` (its own
already-computed content hash plus the exact size/mtime it was checked
against) through to each file's ``ProcessorContext`` -- indexing
optimization plan, Phase P3 / finding F4. This is the plumbing a
processor's own reuse (``documents/pipeline.py``'s ``verified_hash``
call) depends on; a broken link here would silently make that reuse a
no-op everywhere.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.config import RagMonkConfig
from ragmonk.core.models import FileKind
from ragmonk.indexing.coordinator import (
    IndexCoordinator,
    ProcessingOutcome,
    ProcessorContext,
    ProcessorRegistry,
)
from ragmonk.sources.fingerprint import hash_file
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect


def test_processor_context_carries_the_coordinators_verified_identity(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.dat"  # FileKind.UNKNOWN -> whatever's registered below
    target.write_text("hello world")

    seen_contexts: list[ProcessorContext] = []

    def spy_processor(ctx: ProcessorContext) -> ProcessingOutcome:
        seen_contexts.append(ctx)
        from ragmonk.core.models import FileStatus

        return ProcessingOutcome(status=FileStatus.INDEXED)

    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, spy_processor)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(
            conn, "s1", str(root), [], [], RagMonkConfig(), processors=registry
        )
        coord.run()

        assert len(seen_contexts) == 1
        identity = seen_contexts[0].file_identity
        assert identity is not None
        stat = target.stat()
        assert identity.size == stat.st_size
        assert identity.mtime == stat.st_mtime
        assert identity.content_hash == hash_file(target)
    finally:
        conn.close()


def test_identity_is_not_carried_over_to_a_files_next_run(tmp_path: Path) -> None:
    """A file's ``file_identity`` is scoped to the run that computed it
    -- a later run (even one that finds the same file unchanged and
    reuses it via the version-triggered reprocess path) must not
    silently see a stale identity left over from a previous run's
    ``_pending_identities`` dict.
    """
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.dat"
    target.write_text("hello world")

    seen_contexts: list[ProcessorContext] = []

    def spy_processor(ctx: ProcessorContext) -> ProcessingOutcome:
        seen_contexts.append(ctx)
        from ragmonk.core.models import FileStatus

        return ProcessingOutcome(status=FileStatus.INDEXED)

    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, spy_processor)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(
            conn, "s1", str(root), [], [], RagMonkConfig(), processors=registry
        )
        coord.run()
        assert len(seen_contexts) == 1

        # A second, unrelated run over the same unchanged file processes
        # nothing new -- but reusing the same coordinator instance must
        # not leak the first run's ``_pending_identities`` entry into
        # instance state a later, different file could accidentally
        # collide with (same coordinator, hypothetically different
        # source content next time).
        coord.run()
        assert coord._pending_identities == {}
    finally:
        conn.close()
