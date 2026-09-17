"""PDF golden test -- gated behind the ``docling_pdf`` marker because it
exercises Docling's real PDF pipeline, which downloads layout/table-
structure model weights from Hugging Face on first use. Run explicitly
with ``pytest -m docling_pdf``; excluded from the default ``pytest -q``
run (see ``pyproject.toml``'s ``addopts`` and CONTRIBUTING.md).

Also proves the native-``DoclingDocument`` conversion cache
(``document_conversion_cache``, see ``docling_adapter``'s module
docstring): that a first ``convert()`` populates it with the real PDF-
layout document's own JSON serialization, that a second ``convert()``
against the same connection reuses it rather than re-running the real PDF
pipeline (including for a duplicate/renamed file with identical content),
and that a stale ``cache_version`` is treated as a miss.

Search Quality Improvement Plan, Phase 5: also proves ``documents.ocr``'s
"auto" mode really does trigger Docling's real OCR pipeline against a
genuinely textless PDF (``scanned.pdf``, a hand-assembled fixture with an
empty content stream rather than a true raster scan -- see
``generate_fixtures.py``'s ``_make_scanned_pdf`` docstring for why that's
a faithful enough stand-in: it hits the exact "zero extracted text"
condition that triggers OCR, without this project needing an
image-embedding fixture library it otherwise has no use for). Every other
"auto"/"always"/cache-mode-distinguishing behavior is covered, mocked and
model-download-free, in ``test_docling_adapter_ocr.py``.

Search Quality Improvement Plan, Phase 10: also proves real, genuine OCR
text extraction from a raw image (``sample_ocr.png``) via
``documents.image_ocr`` -- the same real OCR engine/model weights as
Phase 5's PDF OCR (RapidOCR, downloaded from ModelScope rather than
Hugging Face, but the same "real network + model download on first use"
shape this marker already exists for), so it reuses this marker rather
than introducing a new one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ragmonk.documents import chunker, docling_adapter, normalizer
from ragmonk.documents.metadata import extract_metadata
from ragmonk.sources.fingerprint import hash_file
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import document_conversion_cache_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"

pytestmark = pytest.mark.docling_pdf


def test_pdf_page_count_without_conversion() -> None:
    # No ML model weights are loaded for this -- see docling_adapter
    # .pdf_page_count -- but it is still exercised only under this marker
    # to keep the default suite's document tests entirely self-contained.
    assert docling_adapter.pdf_page_count(FIXTURES / "sample.pdf") == 1


def test_pdf_converts_to_a_paragraph_with_page_provenance() -> None:
    path = FIXTURES / "sample.pdf"
    doc_format = docling_adapter.detect_format(path)
    assert doc_format.value == "pdf"

    conversion = docling_adapter.convert(path)
    # Native document straight off Docling's real PDF pipeline: real
    # ``prov`` on every item, real ``num_pages()`` -- no override needed.
    assert conversion.document.num_pages() == 1
    normalized = normalizer.normalize(conversion.document, doc_format)
    chunks = chunker.chunk_document(normalized)
    meta = extract_metadata(conversion.document, normalized, doc_format, path)

    assert meta.page_count == 1
    assert meta.is_scanned is False
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.kind == "paragraph"
    assert "Sample PDF Title" in chunk.text
    assert "This is a line of body text in the sample PDF." in chunk.text
    assert chunk.page_start == 1
    assert chunk.page_end == 1


def test_convert_caches_the_native_document_by_content_hash(tmp_path: Path) -> None:
    path = FIXTURES / "sample.pdf"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        assert (
            document_conversion_cache_repo.get(
                conn,
                hash_file(path),
                ocr_used=docling_adapter._OCR_NOT_APPLIED,
                cache_version=docling_adapter._CACHE_VERSION,
            )
            is None
        )

        docling_adapter.convert(path, conn=conn)

        cached = document_conversion_cache_repo.get(
            conn,
            hash_file(path),
            ocr_used=docling_adapter._OCR_NOT_APPLIED,
            cache_version=docling_adapter._CACHE_VERSION,
        )
        assert cached is not None
        assert cached.page_count == 1
        assert cached.serialization_format == docling_adapter._SERIALIZATION_FORMAT
        assert cached.parser_version
        assert "Sample PDF Title" in cached.serialized_document
    finally:
        conn.close()


def test_convert_reuses_cached_document_without_reconverting(tmp_path: Path) -> None:
    path = FIXTURES / "sample.pdf"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        # First call: real, unpatched conversion -- populates the cache.
        first = docling_adapter.convert(path, conn=conn)

        # Second call: the PDF-pipeline singleton must never be touched --
        # a real reconversion would raise here instead of being reused.
        with patch.object(docling_adapter, "_get_converter") as get_converter:
            get_converter.return_value.convert.side_effect = AssertionError(
                "should not reconvert"
            )
            second = docling_adapter.convert(path, conn=conn)

        get_converter.assert_not_called()
        assert second.document.num_pages() == first.document.num_pages() == 1
        assert second.document.export_to_markdown() == first.document.export_to_markdown()
        assert second.document == first.document
    finally:
        conn.close()


def test_duplicate_pdf_with_identical_content_reuses_the_cache(tmp_path: Path) -> None:
    """A byte-identical copy of an already-converted PDF, at a different
    path, must hit the cache too -- ``document_conversion_cache`` is keyed
    by content hash, not file id/path (see ``storage/schema.py``'s
    ``KNOWLEDGE_DB_V6`` docstring).
    """
    original = FIXTURES / "sample.pdf"
    duplicate = tmp_path / "a_duplicate_copy.pdf"
    shutil.copyfile(original, duplicate)
    assert hash_file(original) == hash_file(duplicate)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        docling_adapter.convert(original, conn=conn)

        with patch.object(docling_adapter, "_get_converter") as get_converter:
            get_converter.return_value.convert.side_effect = AssertionError(
                "should not reconvert"
            )
            duplicate_result = docling_adapter.convert(duplicate, conn=conn)

        get_converter.assert_not_called()
        assert duplicate_result.document.num_pages() == 1
    finally:
        conn.close()


def test_moved_pdf_with_identical_content_reuses_the_cache(tmp_path: Path) -> None:
    """A PDF moved/renamed after its first conversion still hits the
    cache -- its bytes, and therefore its content hash, are unchanged.
    """
    working_copy = tmp_path / "before_rename.pdf"
    shutil.copyfile(FIXTURES / "sample.pdf", working_copy)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        docling_adapter.convert(working_copy, conn=conn)

        moved = tmp_path / "after_rename.pdf"
        working_copy.rename(moved)

        with patch.object(docling_adapter, "_get_converter") as get_converter:
            get_converter.return_value.convert.side_effect = AssertionError(
                "should not reconvert"
            )
            moved_result = docling_adapter.convert(moved, conn=conn)

        get_converter.assert_not_called()
        assert moved_result.document.num_pages() == 1
    finally:
        conn.close()


def test_stale_cache_version_causes_reconversion(tmp_path: Path) -> None:
    """A cache row written under an older ``cache_version`` (including,
    conceptually, a pre-Phase-1B row that cached Markdown text instead of
    a serialized ``DoclingDocument`` -- see ``storage/schema.py``'s
    ``KNOWLEDGE_DB_V10`` docstring) must never be handed back as if it
    still matched the current serialization scheme: ``convert()`` must
    reconvert instead of trying to deserialize it.
    """
    path = FIXTURES / "sample.pdf"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        with patch.object(docling_adapter, "_CACHE_VERSION", docling_adapter._CACHE_VERSION - 1):
            docling_adapter.convert(path, conn=conn)

        # The row above was written under a stale version relative to the
        # module's real ``_CACHE_VERSION`` -- a lookup at the real version
        # must miss, so this call has to run the real pipeline again
        # rather than raise from a mocked, must-not-be-called converter.
        stale = document_conversion_cache_repo.get(
            conn,
            hash_file(path),
            ocr_used=docling_adapter._OCR_NOT_APPLIED,
            cache_version=docling_adapter._CACHE_VERSION,
        )
        assert stale is None

        result = docling_adapter.convert(path, conn=conn)
        assert result.document.num_pages() == 1

        fresh = document_conversion_cache_repo.get(
            conn,
            hash_file(path),
            ocr_used=docling_adapter._OCR_NOT_APPLIED,
            cache_version=docling_adapter._CACHE_VERSION,
        )
        assert fresh is not None
    finally:
        conn.close()


def test_auto_mode_runs_real_ocr_on_a_textless_pdf(tmp_path: Path) -> None:
    """End-to-end proof (real Docling pipeline, real OCR engine) that
    ``documents.ocr="auto"`` actually detects a textless PDF and re-runs
    conversion with OCR enabled -- both cache entries (plain and OCR'd)
    end up populated, keyed apart by ``ocr_used``.
    """
    path = FIXTURES / "scanned.pdf"
    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")

        plain_only = docling_adapter.convert(path, conn=conn, ocr_mode="off")
        doc_format = docling_adapter.detect_format(path)
        assert normalizer.normalize(plain_only.document, doc_format).is_scanned is True

        result = docling_adapter.convert(path, conn=conn, ocr_mode="auto")
        assert result.document.num_pages() == 1

        plain_cached = document_conversion_cache_repo.get(
            conn,
            hash_file(path),
            ocr_used=docling_adapter._OCR_NOT_APPLIED,
            cache_version=docling_adapter._CACHE_VERSION,
        )
        ocr_cached = document_conversion_cache_repo.get(
            conn,
            hash_file(path),
            ocr_used=docling_adapter._OCR_APPLIED,
            cache_version=docling_adapter._CACHE_VERSION,
        )
        # Both rows exist independently, under their own `ocr_used` key --
        # not one overwriting the other. Their content happens to be
        # identical for this particular fixture (an empty page has
        # nothing for even a real OCR engine to recover), so this doesn't
        # assert they differ; ``test_docling_adapter_ocr.py`` proves the
        # cache genuinely keeps them apart when they *do* differ.
        assert plain_cached is not None
        assert ocr_cached is not None
    finally:
        conn.close()


def test_image_ocr_enabled_extracts_real_text_via_ocr() -> None:
    """End-to-end proof (real Docling image pipeline, real OCR engine)
    that a raw image -- which has no embedded text layer at all, unlike
    PDF -- genuinely round-trips real, rendered-not-embedded text through
    OCR. ``sample_ocr.png`` (see ``generate_fixtures.py``'s
    ``_make_ocr_image``) draws its words as pixels with Pillow, so
    finding them back here proves RapidOCR actually read the image
    rather than a fixture that happens to carry the text as data.
    """
    path = FIXTURES / "sample_ocr.png"
    doc_format = docling_adapter.detect_format(path)
    assert doc_format.value == "image"

    conversion = docling_adapter.convert(path)
    normalized = normalizer.normalize(conversion.document, doc_format)
    chunks = chunker.chunk_document(normalized)
    meta = extract_metadata(conversion.document, normalized, doc_format, path)

    assert meta.format.value == "image"
    all_text = " ".join(c.text for c in chunks)
    assert "Sample Image Title" in all_text
    assert "OCR body text line here" in all_text
