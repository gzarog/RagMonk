"""Indexing optimization plan V2, Phase P1: code derivation versioning.

``code.processor.code_version_stamp`` is compared against a previously
indexed file's stored ``parser_version`` column the same way the document
pipeline's version stamp already is (``indexing/incremental.
decide_reprocessing``). These tests exercise that end-to-end through
``IndexCoordinator``, mirroring the style of
``test_index_coordinator_parallel_code.py``.
"""

from __future__ import annotations

from pathlib import Path

import ragmonk.code.processor as code_processor_module
from ragmonk.code.processor import code_version_stamp
from ragmonk.core.config import IndexingConfig, RagMonkConfig
from ragmonk.core.models import FileKind
from ragmonk.indexing.coordinator import IndexCoordinator
from ragmonk.indexing.incremental import ReprocessDecision, VersionStamp, decide_reprocessing
from ragmonk.indexing.runner import build_processor_registry
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import entities_repo, files_repo
from ragmonk.storage.sqlite import connect


def _config() -> RagMonkConfig:
    config = RagMonkConfig(indexing=IndexingConfig(code_extraction_workers=1))
    disabled_documents = config.documents.model_copy(update={"enabled": False})
    return config.model_copy(update={"documents": disabled_documents})


def _write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "b.py").write_text("def b():\n    return 2\n")


def test_code_version_stamp_is_independent_of_embedding_axes() -> None:
    stamp = code_version_stamp()
    assert stamp.parser_version == code_processor_module.CODE_DERIVATION_VERSION
    # No chunker axis for code (see code_version_stamp's docstring), and
    # embedding invalidation is deliberately routed through its own
    # narrower path (CODE_EMBEDDING_TEXT_VERSION), not this stamp.
    assert stamp.chunker_version is None
    assert stamp.embedding_model_id is None
    assert stamp.embedding_text_version is None


def test_bumped_derivation_version_reprocesses_unchanged_code_exactly_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    _write_project(root)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config()
        registry = build_processor_registry(config)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)

        first = coord.run()
        assert first.indexed == 2
        assert first.unchanged == 0
        entity_count_before = len(entities_repo.list_all(conn))
        stamped = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
        for record in stamped.values():
            assert record.parser_version == code_processor_module.CODE_DERIVATION_VERSION

        # A normal warm run with unchanged source and unchanged version
        # performs no code reprocessing at all.
        second = coord.run()
        assert second.indexed == 0
        assert second.unchanged == 2

        # Bump the derivation version as if the parser/extractor code
        # itself changed -- source bytes are still untouched.
        original_version = code_processor_module.CODE_DERIVATION_VERSION
        code_processor_module.CODE_DERIVATION_VERSION = "2"
        try:
            third = coord.run()
            # Exactly one full reprocess of both unchanged-content files.
            assert third.indexed == 2
            assert third.unchanged == 0
            restamped = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
            for record in restamped.values():
                assert record.parser_version == "2"
            # generation bumped -- confirms a genuine reprocess happened,
            # not merely a stamp update.
            for path, before in stamped.items():
                assert restamped[path].generation > before.generation

            # A further run at the same (now current) version is warm
            # again: no more reprocessing.
            fourth = coord.run()
            assert fourth.indexed == 0
            assert fourth.unchanged == 2
        finally:
            code_processor_module.CODE_DERIVATION_VERSION = original_version

        # Entities were actually regenerated, not merely re-stamped in
        # place, and there is no duplication from a broken
        # delete-and-reinsert across the version-triggered reprocess.
        entity_count_after = len(entities_repo.list_all(conn))
        assert entity_count_after == entity_count_before
    finally:
        conn.close()


def test_null_stored_version_on_a_preexisting_row_does_not_force_reprocessing(
    tmp_path: Path,
) -> None:
    """Migration safety: a row indexed before this phase shipped (or by
    any processor without a version provider) has ``parser_version =
    NULL``. ``VersionStamp``'s docstring is explicit that ``None`` on the
    *stored* side must never itself be treated as stale -- only a stored
    value that is actually known and disagrees with the current one is.
    This is what lets P1 ship without forcing every previously-indexed
    code file in an existing project to reprocess on the next run.
    """
    existing = VersionStamp(
        parser_version=None,
        chunker_version=None,
        embedding_model_id=None,
        embedding_text_version=None,
    )
    current = code_version_stamp()
    assert decide_reprocessing(existing, current) is ReprocessDecision.NONE


def test_preexisting_rows_at_the_schema_default_are_not_reprocessed(tmp_path: Path) -> None:
    """Migration compatibility for real installs: ``files.parser_version``
    is ``NOT NULL DEFAULT '1'`` (``storage/schema.py``'s ``KNOWLEDGE_DB_V1``)
    -- every code file indexed by any pre-P1 build (when CODE registered no
    version_provider at all, so nothing ever wrote this column) already
    sits at that schema default, never ``NULL``. ``CODE_DERIVATION_VERSION``
    is deliberately chosen to start at ``"1"`` so it matches that existing
    default: upgrading to a P1 build must not force a reprocess of an
    entire pre-existing project just because CODE now has a version
    provider.
    """
    assert code_processor_module.CODE_DERIVATION_VERSION == "1"

    root = tmp_path / "source"
    _write_project(root)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        # Simulate a pre-P1 install: index CODE with the *old*, plain
        # ``code_processor`` and no version_provider at all -- exactly
        # ProcessorRegistry's state before this phase's runner.py change.
        pre_p1_config = _config()
        pre_p1_registry = build_processor_registry(pre_p1_config)
        pre_p1_registry._version_providers.pop(FileKind.CODE, None)  # noqa: SLF001
        coord = IndexCoordinator(
            conn, "s1", str(root), [], [], pre_p1_config, processors=pre_p1_registry
        )
        coord.run()

        def _by_path(conn_) -> dict[str, tuple[int, str | None]]:  # noqa: ANN001
            return {
                f.path: (f.generation, f.parser_version)
                for f in files_repo.list_by_source(conn_, "s1")
            }

        before = _by_path(conn)
        assert all(version == "1" for _, version in before.values())

        # Now index again with the real, P1-aware registry (version
        # provider registered) -- a warm run should see no reprocessing
        # at all, since the stored default already matches.
        p1_config = _config()
        p1_registry = build_processor_registry(p1_config)
        p1_coord = IndexCoordinator(
            conn, "s1", str(root), [], [], p1_config, processors=p1_registry
        )
        result = p1_coord.run()

        assert result.indexed == 0
        assert result.unchanged == 2
        assert before == _by_path(conn)
    finally:
        conn.close()
