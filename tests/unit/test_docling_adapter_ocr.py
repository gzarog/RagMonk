"""``documents.ocr``'s "off"/"auto"/"always" behavior in
``docling_adapter``, and the cache's ``ocr_used`` key -- entirely mocked
at the ``_get_converter``/``_get_ocr_converter``/``_run_conversion`` seam
so none of this touches Docling's real PDF pipeline (no model download,
no network): see ``test_docling_pdf.py`` for the real, ``docling_pdf``-
marked end-to-end proof, including a real scanned-PDF-triggers-OCR case.

Each test uses a distinct sentinel "converter" object per pipeline (plain
vs. OCR) so a mocked ``_run_conversion`` can tell, from its own
``converter`` argument alone, which pipeline a given call belongs to --
without ever constructing (or even importing) a real
``docling.document_converter.DocumentConverter``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.document import DoclingDocument

from ragpilot.documents import docling_adapter
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import document_conversion_cache_repo
from ragpilot.storage.sqlite import connect

_PLAIN = "plain-converter-sentinel"
_OCR = "ocr-converter-sentinel"


def _prov(page_no: int) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page_no, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1)
    )


def _dense_document(page_count: int = 1) -> DoclingDocument:
    """Plenty of real text on every page -- never looks scanned."""
    doc = DoclingDocument(name="dense")
    for page_no in range(1, page_count + 1):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
        doc.add_text(DocItemLabel.TEXT, "Real extracted body text. " * 10, prov=_prov(page_no))
    return doc


def _scanned_document(page_count: int = 1) -> DoclingDocument:
    """Pages with no text items at all -- Docling's own observable proxy
    for "nothing extracted here", exactly what an image-only PDF yields
    with OCR off.
    """
    doc = DoclingDocument(name="scanned")
    for page_no in range(1, page_count + 1):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
    return doc


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 fake content, never actually parsed by Docling in this test")
    return path


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def _patched(
    plain_document: DoclingDocument,
    ocr_document: DoclingDocument | None,
    *,
    ocr_raises: bool = False,
):  # noqa: ANN201
    """Context manager stack: ``_get_converter``/``_get_ocr_converter``
    return the two sentinels above; ``_run_conversion`` returns
    ``plain_document``/``ocr_document`` depending on which sentinel it was
    called with (or raises, for the OCR sentinel, when ``ocr_raises``).
    """

    def fake_run_conversion(converter: str, path: Path, source: str) -> DoclingDocument:
        if converter == _PLAIN:
            return plain_document
        assert converter == _OCR
        if ocr_raises:
            raise RuntimeError("no OCR engine installed")
        assert ocr_document is not None
        return ocr_document

    return (
        patch.object(docling_adapter, "_get_converter", return_value=_PLAIN),
        patch.object(docling_adapter, "_get_ocr_converter", return_value=_OCR),
        patch.object(docling_adapter, "_run_conversion", side_effect=fake_run_conversion),
    )


def test_off_mode_never_triggers_ocr_even_for_a_scanned_looking_pdf(pdf_path: Path, conn) -> None:  # noqa: ANN001
    """Regression guard: Phase 3's original, only behavior -- OCR must
    never run under "off", even when the plain pass looks exactly like
    something "auto" would OCR.
    """
    scanned = _scanned_document()
    get_converter, get_ocr_converter, run_conversion = _patched(scanned, None)
    with get_converter, get_ocr_converter as ocr_mock, run_conversion:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="off")

    assert result.document is scanned
    ocr_mock.assert_not_called()
    cached = document_conversion_cache_repo.get(
        conn,
        docling_adapter.hash_file(pdf_path),
        ocr_used=docling_adapter._OCR_NOT_APPLIED,
        cache_version=docling_adapter._CACHE_VERSION,
    )
    assert cached is not None


def test_default_ocr_mode_is_off_for_every_existing_caller(pdf_path: Path) -> None:
    """``convert()``'s own default -- what every pre-Phase-5 caller (unit
    tests included) that never passes ``ocr_mode`` still gets -- must stay
    "off", byte-identical to Phase 3.
    """
    scanned = _scanned_document()
    get_converter, get_ocr_converter, run_conversion = _patched(scanned, None)
    with get_converter, get_ocr_converter as ocr_mock, run_conversion:
        result = docling_adapter.convert(pdf_path)  # no conn, no ocr_mode

    assert result.document is scanned
    ocr_mock.assert_not_called()


def test_auto_mode_does_not_trigger_ocr_for_a_native_text_pdf(pdf_path: Path, conn) -> None:  # noqa: ANN001
    dense = _dense_document()
    get_converter, get_ocr_converter, run_conversion = _patched(dense, None)
    with get_converter, get_ocr_converter as ocr_mock, run_conversion as run_mock:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="auto")

    assert result.document is dense
    ocr_mock.assert_not_called()
    # Exactly one conversion -- the plain pass -- no second pipeline run
    # for the common "OCR was never needed" case.
    assert run_mock.call_count == 1


def test_auto_mode_triggers_ocr_for_a_scanned_looking_pdf(pdf_path: Path, conn) -> None:  # noqa: ANN001
    scanned = _scanned_document()
    ocr_result = _dense_document()  # OCR "recovers" real text
    get_converter, get_ocr_converter, run_conversion = _patched(scanned, ocr_result)
    with get_converter, get_ocr_converter as ocr_mock, run_conversion:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="auto")

    assert result.document is ocr_result
    ocr_mock.assert_called_once()

    plain_cached = document_conversion_cache_repo.get(
        conn,
        docling_adapter.hash_file(pdf_path),
        ocr_used=docling_adapter._OCR_NOT_APPLIED,
        cache_version=docling_adapter._CACHE_VERSION,
    )
    ocr_cached = document_conversion_cache_repo.get(
        conn,
        docling_adapter.hash_file(pdf_path),
        ocr_used=docling_adapter._OCR_APPLIED,
        cache_version=docling_adapter._CACHE_VERSION,
    )
    assert plain_cached is not None
    assert ocr_cached is not None


def test_always_mode_runs_ocr_unconditionally_without_a_detection_pass(  # noqa: ANN001
    pdf_path: Path, conn
) -> None:
    """"always" must skip the two-pass detection entirely -- even a plain
    pass that would *not* have triggered "auto" must never run, since
    "always" already knows it wants OCR.
    """
    dense = _dense_document()  # would never trigger auto's detection
    ocr_result = _dense_document()
    get_converter, get_ocr_converter, run_conversion = _patched(dense, ocr_result)
    with get_converter as plain_mock, get_ocr_converter, run_conversion as run_mock:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="always")

    assert result.document is ocr_result
    plain_mock.assert_not_called()
    assert run_mock.call_count == 1


def test_ocr_cache_does_not_serve_off_mode_requests(pdf_path: Path, conn) -> None:  # noqa: ANN001
    """A page cached under "always"/OCR must never satisfy a later "off"
    request for the same content hash.
    """
    ocr_result = _dense_document()
    get_converter, get_ocr_converter, run_conversion = _patched(_scanned_document(), ocr_result)
    with get_converter, get_ocr_converter, run_conversion:
        docling_adapter.convert(pdf_path, conn=conn, ocr_mode="always")

    # Now request "off" for the same file -- must reconvert with the
    # plain pipeline rather than being handed the OCR'd row back.
    plain = _dense_document()
    get_converter2, get_ocr_converter2, run_conversion2 = _patched(plain, None)
    with get_converter2, get_ocr_converter2 as ocr_mock2, run_conversion2:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="off")

    assert result.document is plain
    ocr_mock2.assert_not_called()


def test_off_mode_cache_does_not_satisfy_a_later_auto_triggered_ocr_request(
    pdf_path: Path, conn  # noqa: ANN001
) -> None:
    """The reverse: a plain-pipeline row cached under "off" must not be
    handed back for a later "auto" request that actually needs OCR.
    """
    scanned = _scanned_document()
    get_converter, get_ocr_converter, run_conversion = _patched(scanned, None)
    with get_converter, get_ocr_converter, run_conversion:
        docling_adapter.convert(pdf_path, conn=conn, ocr_mode="off")

    ocr_result = _dense_document()
    get_converter2, get_ocr_converter2, run_conversion2 = _patched(scanned, ocr_result)
    with get_converter2, get_ocr_converter2 as ocr_mock2, run_conversion2:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="auto")

    assert result.document is ocr_result
    ocr_mock2.assert_called_once()


def test_always_mode_falls_back_to_plain_conversion_when_ocr_engine_is_unavailable(
    pdf_path: Path, conn  # noqa: ANN001
) -> None:
    """Graceful degradation: an OCR-pipeline failure (standing in for "no
    OCR engine installed") must never fail the whole file -- "always"
    falls back to the plain pipeline instead.
    """
    plain = _dense_document()
    get_converter, get_ocr_converter, run_conversion = _patched(plain, None, ocr_raises=True)
    with get_converter, get_ocr_converter, run_conversion:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="always")

    assert result.document is plain
    cached = document_conversion_cache_repo.get(
        conn,
        docling_adapter.hash_file(pdf_path),
        ocr_used=docling_adapter._OCR_NOT_APPLIED,
        cache_version=docling_adapter._CACHE_VERSION,
    )
    assert cached is not None


def test_auto_mode_keeps_the_plain_result_when_ocr_engine_is_unavailable(
    pdf_path: Path, conn  # noqa: ANN001
) -> None:
    scanned = _scanned_document()
    get_converter, get_ocr_converter, run_conversion = _patched(scanned, None, ocr_raises=True)
    with get_converter, get_ocr_converter, run_conversion:
        result = docling_adapter.convert(pdf_path, conn=conn, ocr_mode="auto")

    assert result.document is scanned
