"""``IndexCoordinator``'s bounded parallel document extraction (indexing
optimization plan V2, Phase P2): opt-in via ``indexing.
document_extraction_workers`` (default 1, fully serial). Mirrors
``test_index_coordinator_parallel_code.py``'s coverage for CODE.

Uses plain ``.txt`` fixtures (Docling's rule-based, no-ML-model text
backend) so these stay fast and need no model download -- see
``pyproject.toml``'s ``docling_pdf`` marker docstring for which formats
that applies to.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.core.config import IndexingConfig, RagMonkConfig
from ragmonk.core.models import FileKind
from ragmonk.documents.pipeline import document_processor, prepare_document, publish_document
from ragmonk.indexing import retry
from ragmonk.indexing.coordinator import IndexCoordinator, ProcessorRegistry, raw_processor
from ragmonk.indexing.runner import build_processor_registry
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo, files_repo
from ragmonk.storage.sqlite import connect

FIXTURE_TEXT = (Path(__file__).parent.parent / "fixtures" / "documents" / "simple.txt").read_text()


def _config(*, document_extraction_workers: int = 1) -> RagMonkConfig:
    return RagMonkConfig(
        indexing=IndexingConfig(document_extraction_workers=document_extraction_workers)
    )


def _documents_only_registry(
    *,
    prepare=prepare_document,
    publish=publish_document,  # noqa: ANN001
) -> ProcessorRegistry:
    """A registry that only registers DOCUMENT (with the given prepare/
    publish, defaulting to the real ones) and falls back to
    ``raw_processor`` for every other kind -- lets a test inject a spy
    prepare function directly, with no monkeypatch needed. Mirrors
    ``test_index_coordinator_parallel_code.py``'s
    ``_code_only_registry``.
    """
    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    registry.register(FileKind.DOCUMENT, document_processor, prepare=prepare, publish=publish)
    return registry


def _write_project(root: Path, n_files: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (root / f"doc_{i}.txt").write_text(f"{FIXTURE_TEXT}\n\nfile number {i}\n")


def test_serial_and_parallel_produce_equivalent_documents_and_sections(
    tmp_path: Path,
) -> None:
    serial_root = tmp_path / "serial"
    parallel_root = tmp_path / "parallel"
    _write_project(serial_root, 6)
    _write_project(parallel_root, 6)

    serial_conn = connect(tmp_path / "serial.db")
    parallel_conn = connect(tmp_path / "parallel.db")
    try:
        apply_migrations(serial_conn, "knowledge")
        apply_migrations(parallel_conn, "knowledge")

        serial_registry = build_processor_registry(_config(document_extraction_workers=1))
        parallel_registry = build_processor_registry(_config(document_extraction_workers=4))

        serial_coord = IndexCoordinator(
            serial_conn,
            "s1",
            str(serial_root),
            [],
            [],
            _config(document_extraction_workers=1),
            processors=serial_registry,
        )
        parallel_coord = IndexCoordinator(
            parallel_conn,
            "s1",
            str(parallel_root),
            [],
            [],
            _config(document_extraction_workers=4),
            processors=parallel_registry,
        )
        serial_result = serial_coord.run()
        parallel_result = parallel_coord.run()

        assert serial_result.indexed == parallel_result.indexed == 6
        assert serial_result.failed == parallel_result.failed == 0

        def _summary(conn):  # noqa: ANN001, ANN202
            docs = documents_repo.list_all(conn)
            out = []
            for doc in sorted(docs, key=lambda d: d.file_id):
                units = documents_repo.list_units_by_file(conn, doc.file_id)
                out.append(
                    (
                        doc.format.value,
                        doc.page_count,
                        doc.section_count,
                        doc.paragraph_count,
                        doc.table_count,
                        sorted(
                            (u.kind.value, u.text, tuple(u.heading_path), u.page_start, u.page_end)
                            for u in units
                        ),
                    )
                )
            return out

        serial_summary = _summary(serial_conn)
        parallel_summary = _summary(parallel_conn)
        assert len(serial_summary) == len(parallel_summary) == 6
        assert sorted(serial_summary) == sorted(parallel_summary)

        # FTS rows: same set of searchable texts either way.
        serial_fts = sorted(
            row["body"] for row in documents_repo.search_fts(serial_conn, "file number", limit=50)
        )
        parallel_fts = sorted(
            row["body"] for row in documents_repo.search_fts(parallel_conn, "file number", limit=50)
        )
        assert len(serial_fts) == len(parallel_fts) > 0
        assert serial_fts == parallel_fts
    finally:
        serial_conn.close()
        parallel_conn.close()


def test_a_failing_document_is_isolated_under_parallel_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real retry/backoff needs several index runs before a failure is
    # permanent; force it on the first attempt so this test can assert
    # `failed` within one `coord.run()` call.
    monkeypatch.setattr(coordinator_module.retry, "is_permanent", lambda attempt: True)

    root = tmp_path / "source"
    _write_project(root, 3)
    # A .docx with corrupted/non-zip bytes: Docling raises
    # DocumentConversionError on it, exactly like any other genuine
    # conversion failure -- isolated by the coordinator's existing
    # per-file retry/backoff, same as a CODE syntax error.
    (root / "broken.docx").write_bytes(b"not a real docx file")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(document_extraction_workers=3)
        registry = build_processor_registry(config)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        assert result.failed == 1
        assert result.indexed == 3
        remaining = {f.path: f.status.value for f in files_repo.list_by_source(conn, "s1")}
        assert any(p.endswith("broken.docx") and s == "failed" for p, s in remaining.items())
        for i in range(3):
            assert any(p.endswith(f"doc_{i}.txt") and s == "indexed" for p, s in remaining.items())
    finally:
        conn.close()


def test_concurrency_stays_within_the_configured_worker_bound(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write_project(root, 10)

    max_concurrent = 0
    current = 0
    lock = threading.Lock()

    def spying_prepare_document(ctx, *, cache_conn):  # noqa: ANN001, ANN202
        nonlocal max_concurrent, current
        with lock:
            current += 1
            max_concurrent = max(max_concurrent, current)
        try:
            time.sleep(0.02)  # widen the window so overlap is observable
            return prepare_document(ctx, cache_conn=cache_conn)
        finally:
            with lock:
                current -= 1

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(document_extraction_workers=3)
        registry = _documents_only_registry(prepare=spying_prepare_document)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        assert result.indexed == 10
        assert max_concurrent <= 3
        assert max_concurrent > 1  # actually ran concurrently, not accidentally serial
    finally:
        conn.close()


def test_document_changed_during_parallel_processing_is_safely_retried(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.txt"
    target.write_text(FIXTURE_TEXT)
    (root / "b.txt").write_text(f"{FIXTURE_TEXT}\n\nb\n")

    def mutating_prepare_document(ctx, *, cache_conn):  # noqa: ANN001, ANN202
        if ctx.path.name == "a.txt":
            # Simulate a concurrent write landing between claim and
            # publish: the coordinator captured a.txt's identity before
            # this call; mutate it now so publish's post-check sees a
            # mismatch.
            time.sleep(0.01)
            ctx.path.write_text("mutated while processing")
        return prepare_document(ctx, cache_conn=cache_conn)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(document_extraction_workers=2)
        registry = _documents_only_registry(prepare=mutating_prepare_document)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        records = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
        a_record = next(f for p, f in records.items() if p.endswith("a.txt"))
        assert a_record.status.value in {"retry", "queued"}
        assert result.indexed == 1  # only b.txt made it through this pass
        # Nothing was published for a.txt's stale extraction.
        assert documents_repo.get_document_by_file(conn, a_record.id) is None
    finally:
        conn.close()


def test_worker_restart_between_runs_cannot_publish_a_stale_generation(tmp_path: Path) -> None:
    """A worker crash mid-run leaves the claimed job's file in RETRY
    (real exponential backoff -- ``retry.backoff_seconds`` -- schedules
    its next attempt slightly in the future, so it is *not* immediately
    reclaimed within the same ``run()`` call), never PUBLISHED under a
    stale generation. Re-running the pass after that backoff elapses
    (simulating a process restart picking the job back up) must converge
    to exactly one correct generation per file, not a partial or doubled
    one.
    """
    root = tmp_path / "source"
    _write_project(root, 4)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(document_extraction_workers=2)
        registry = build_processor_registry(config)

        # First "process": crashes after prepare, before this run's
        # publish ever executes -- simulated by raising inside publish
        # for every file once, on the first attempt only.
        raise_once = {"armed": True}
        real_publish = publish_document

        def flaky_publish(ctx, prepared):  # noqa: ANN001, ANN202
            if raise_once["armed"]:
                raise_once["armed"] = False
                raise RuntimeError("simulated worker crash before publish")
            return real_publish(ctx, prepared)

        crashy_registry = build_processor_registry(config)
        crashy_registry.register(
            FileKind.DOCUMENT,
            document_processor,
            prepare=crashy_registry.get_prepare(FileKind.DOCUMENT),
            publish=flaky_publish,
        )
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=crashy_registry)
        first = coord.run()
        # Exactly one file hit the simulated crash and is left for retry;
        # the rest published normally.
        assert first.failed == 0  # not yet permanent -- retry, not failed
        assert first.indexed == 3

        # Nothing was published for the crashed file's attempt.
        crashed_path = next(
            p
            for p, f in {f.path: f for f in files_repo.list_by_source(conn, "s1")}.items()
            if f.status.value == "retry"
        )
        crashed_record = next(
            f for f in files_repo.list_by_source(conn, "s1") if f.path == crashed_path
        )
        assert documents_repo.get_document_by_file(conn, crashed_record.id) is None

        # Real exponential backoff (retry.backoff_seconds) means the
        # crashed file's job is not eligible for reclaim yet.
        time.sleep(retry.backoff_seconds(1) + 0.1)

        # "Restart": run again with the real (non-crashy) registry.
        restart_coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        second = restart_coord.run()
        assert second.indexed == 1  # the one file that crashed last time

        # Converges to exactly one document row (one generation) per file
        # -- no duplicate/partial publication from the crashed attempt.
        docs_by_file: dict[str, int] = {}
        for doc in documents_repo.list_all(conn):
            docs_by_file[doc.file_id] = docs_by_file.get(doc.file_id, 0) + 1
        assert all(count == 1 for count in docs_by_file.values())
        assert len(docs_by_file) == 4
    finally:
        conn.close()
