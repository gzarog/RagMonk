"""``document_processor``'s use of ``ProcessorContext.file_identity``
(indexing optimization plan, Phase P3 / finding F4): reuses the
coordinator's already-computed digest instead of re-hashing the whole
file itself, and detects a file that changed between the coordinator's
scan and this processor finishing its work rather than silently
publishing stale derived content.

Uses plain ``.txt`` fixtures (Docling's rule-based, no-ML-model text
backend) so these stay fast and need no model download -- see
``pyproject.toml``'s ``docling_pdf`` marker docstring for which formats
that applies to.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from ragmonk.core.errors import ContentChangedDuringProcessingError
from ragmonk.core.models import FileKind, FileRecord, FileStatus
from ragmonk.documents.pipeline import document_processor
from ragmonk.indexing.coordinator import ProcessorContext
from ragmonk.sources import fingerprint as fingerprint_module
from ragmonk.sources.fingerprint import FileIdentity, hash_file
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _make_conn(tmp_path: Path):  # noqa: ANN201 - test helper
    conn = connect(tmp_path / "knowledge.db")
    apply_migrations(conn, "knowledge")
    return conn


def _make_file_record(conn, path: Path) -> FileRecord:  # noqa: ANN001 - test helper
    stat = path.stat()
    record = FileRecord(
        id=uuid.uuid4().hex,
        source_id="s1",
        path=str(path),
        kind=FileKind.DOCUMENT,
        size=stat.st_size,
        mtime=stat.st_mtime,
        content_hash=None,
        status=FileStatus.QUEUED,
        generation=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    files_repo.insert(conn, record)
    return record


def test_document_processor_reuses_verified_identity_without_rehashing(tmp_path: Path) -> None:
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text((FIXTURES / "simple.txt").read_text())
    stat = doc_path.stat()

    conn = _make_conn(tmp_path)
    try:
        record = _make_file_record(conn, doc_path)
        identity = FileIdentity(
            content_hash=hash_file(doc_path), size=stat.st_size, mtime=stat.st_mtime
        )
        ctx = ProcessorContext(
            path=doc_path,
            size=stat.st_size,
            kind=FileKind.DOCUMENT,
            max_size_bytes=10_000_000,
            conn=conn,
            source_id="s1",
            file_id=record.id,
            next_generation=1,
            file_identity=identity,
        )

        calls = 0
        original_hash_file = fingerprint_module.hash_file

        def _counting_hash_file(path, algorithm="sha256"):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            return original_hash_file(path, algorithm)

        fingerprint_module.hash_file = _counting_hash_file
        try:
            outcome = document_processor(ctx)
        finally:
            fingerprint_module.hash_file = original_hash_file

        assert outcome.status is FileStatus.INDEXED
        # verified_hash trusted the supplied identity (stat matched) --
        # no fallback re-hash of the whole file was needed.
        assert calls == 0
    finally:
        conn.close()


def test_document_processor_trusts_identity_content_hash_when_stat_matches(
    tmp_path: Path,
) -> None:
    """Mirrors ``verified_hash``'s own contract at the document-processor
    level: the *stat* is what's verified, not the hash string itself --
    a caller (the coordinator) that already verified it is trusted
    as-is when the current stat still matches, with no independent
    re-read to double-check the hash's actual content.
    """
    from ragmonk.storage.repositories import documents_repo

    doc_path = tmp_path / "doc.txt"
    doc_path.write_text((FIXTURES / "simple.txt").read_text())
    stat = doc_path.stat()

    conn = _make_conn(tmp_path)
    try:
        record = _make_file_record(conn, doc_path)
        identity = FileIdentity(
            content_hash="a-verified-but-arbitrary-hash", size=stat.st_size, mtime=stat.st_mtime
        )
        ctx = ProcessorContext(
            path=doc_path,
            size=stat.st_size,
            kind=FileKind.DOCUMENT,
            max_size_bytes=10_000_000,
            conn=conn,
            source_id="s1",
            file_id=record.id,
            next_generation=1,
            file_identity=identity,
        )

        outcome = document_processor(ctx)
        assert outcome.status is FileStatus.INDEXED
        stored = documents_repo.get_document_by_file(conn, record.id)
        assert stored is not None
        assert stored.content_hash == "a-verified-but-arbitrary-hash"
    finally:
        conn.close()


def test_document_processor_detects_content_changed_during_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text((FIXTURES / "simple.txt").read_text())
    stat = doc_path.stat()

    conn = _make_conn(tmp_path)
    try:
        record = _make_file_record(conn, doc_path)
        identity = FileIdentity(
            content_hash=hash_file(doc_path), size=stat.st_size, mtime=stat.st_mtime
        )
        ctx = ProcessorContext(
            path=doc_path,
            size=stat.st_size,
            kind=FileKind.DOCUMENT,
            max_size_bytes=10_000_000,
            conn=conn,
            source_id="s1",
            file_id=record.id,
            next_generation=1,
            file_identity=identity,
        )

        # Simulate a concurrent write landing between the coordinator's
        # scan (when `identity` was captured) and this processor
        # finishing its conversion: the file on disk now differs.
        original_normalize = __import__(
            "ragmonk.documents.normalizer", fromlist=["normalize"]
        ).normalize

        def _mutate_then_normalize(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            time.sleep(0.01)  # ensure a distinct mtime on filesystems with coarse resolution
            doc_path.write_text("mutated while processing")
            return original_normalize(*args, **kwargs)

        monkeypatch.setattr(
            "ragmonk.documents.pipeline.normalizer.normalize", _mutate_then_normalize
        )

        with pytest.raises(ContentChangedDuringProcessingError):
            document_processor(ctx)

        # Nothing was published for the stale extraction.
        from ragmonk.storage.repositories import documents_repo

        assert documents_repo.count_all(conn) == 0
    finally:
        conn.close()
