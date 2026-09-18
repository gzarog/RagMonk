from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import FileKind
from ragmonk.sources.detector import classify


def test_python_file_is_code() -> None:
    assert classify(Path("app.py")) is FileKind.CODE


def test_markdown_file_is_document() -> None:
    assert classify(Path("README.md")) is FileKind.DOCUMENT


def test_unknown_extension_is_unknown() -> None:
    assert classify(Path("data.bin")) is FileKind.UNKNOWN


def test_phase_10_document_extensions_are_recognized() -> None:
    """Search Quality Improvement Plan, Phase 10: these extensions are
    all detected as document-kind, even though not every one is
    actually converted by ``docling_adapter`` -- see that module's
    ``EXTENSION_TO_FORMAT``/``FORMATS_REQUIRING_IMAGE_OCR`` for which
    ones genuinely are.
    """
    for name in (
        "data.csv",
        "doc.odt",
        "sheet.ods",
        "deck.odp",
        "book.epub",
        "scan.png",
        "photo.jpg",
        "photo.jpeg",
        "page.tif",
        "page.tiff",
    ):
        assert classify(Path(name)) is FileKind.DOCUMENT, name
