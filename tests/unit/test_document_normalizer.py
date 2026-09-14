"""Golden tests: Docling conversion -> normalize -> chunk, per format,
asserting RAGpilot's NORMALIZED output (not Docling's internal JSON) per
the blueprint's "Docling Fixtures" guidance.

Every format here (DOCX/PPTX/XLSX/HTML/Markdown/TXT/EML) is converted by
Docling's rule-based backends -- no layout/table-structure model download,
so none of these need the ``docling_pdf`` marker.
"""

from __future__ import annotations

from pathlib import Path

from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.document import DoclingDocument

from ragpilot.core.models import DocumentFormat
from ragpilot.documents import chunker, docling_adapter, normalizer
from ragpilot.documents.metadata import extract_metadata

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _prov(page_no: int) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page_no, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1)
    )


def _convert(name: str):  # noqa: ANN201 - test helper
    path = FIXTURES / name
    doc_format = docling_adapter.detect_format(path)
    conversion = docling_adapter.convert(path)
    normalized = normalizer.normalize(conversion.document, doc_format)
    chunks = chunker.chunk_document(normalized)
    meta = extract_metadata(conversion.document, normalized, doc_format, path)
    return doc_format, normalized, chunks, meta


def test_txt_normalizes_to_a_single_paragraph() -> None:
    doc_format, normalized, chunks, meta = _convert("simple.txt")
    assert doc_format.value == "txt"
    assert meta.page_count is None
    assert len(chunks) == 1
    assert chunks[0].kind == "paragraph"
    assert "Plain text document." in chunks[0].text
    assert "It has two paragraphs." in chunks[0].text


def test_markdown_heading_paragraph_and_table() -> None:
    _, normalized, chunks, meta = _convert("simple.md")
    assert meta.title == "Title Heading"
    kinds = [c.kind for c in chunks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph", "table"]

    top_heading, intro, sub_heading, body, table = chunks
    assert top_heading.text == "Title Heading"
    assert top_heading.heading_level == 0
    assert top_heading.heading_path == ()
    assert intro.text == "Some intro paragraph text."
    assert intro.heading_path == ("Title Heading",)
    assert sub_heading.text == "Section One"
    assert sub_heading.heading_level == 1
    assert sub_heading.heading_path == ("Title Heading",)
    assert body.text == "Paragraph in section one."
    assert body.heading_path == ("Title Heading", "Section One")

    assert table.kind == "table"
    assert table.table_rows == (("Name", "Value"), ("a", "1"), ("b", "2"))
    assert table.heading_path == ("Title Heading", "Section One")
    # Provenance: parent_index links every unit back to its nearest
    # enclosing heading's position in this same chunk list.
    assert chunks[table.parent_index].text == "Section One"


def test_html_heading_paragraph_and_table() -> None:
    _, _, chunks, meta = _convert("simple.html")
    assert meta.title == "Main Heading"
    kinds = [c.kind for c in chunks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph", "table"]
    assert chunks[0].text == "Main Heading"
    assert chunks[1].text == "First paragraph."
    assert chunks[2].text == "Sub Heading"
    assert chunks[3].text == "Second paragraph."
    assert chunks[4].table_rows == (("Col A", "Col B"), ("1", "2"))
    assert chunks[4].heading_path == ("Main Heading", "Sub Heading")


def test_eml_subject_becomes_title_and_body_is_a_paragraph() -> None:
    _, _, chunks, meta = _convert("sample.eml")
    assert meta.title == "Sample Email Subject"
    assert meta.format.value == "eml"
    assert chunks[0].kind == "heading"
    assert chunks[0].text == "Sample Email Subject"
    body_text = "\n\n".join(c.text for c in chunks[1:])
    assert "This is the body of the sample email." in body_text
    assert "It has two paragraphs of content." in body_text


def test_docx_heading_levels_and_table() -> None:
    _, _, chunks, meta = _convert("document.docx")
    assert meta.title == "Doc Title"
    kinds = [c.kind for c in chunks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph", "table"]
    assert chunks[0].heading_level == 1
    assert chunks[2].heading_level == 2
    assert chunks[2].heading_path == ("Doc Title",)
    assert chunks[4].table_rows == (("Name", "Value"), ("alpha", "1"))


def test_pptx_title_and_bullets() -> None:
    _, _, chunks, meta = _convert("presentation.pptx")
    assert meta.title == "Presentation Title"
    assert chunks[0].kind == "heading"
    assert chunks[0].text == "Presentation Title"
    assert "First bullet point" in chunks[1].text
    assert "Second bullet point" in chunks[1].text


def test_xlsx_is_a_single_table_with_no_heading() -> None:
    _, _, chunks, meta = _convert("spreadsheet.xlsx")
    assert meta.page_count == 1
    assert len(chunks) == 1
    assert chunks[0].kind == "table"
    assert chunks[0].table_rows == (
        ("Name", "Value"),
        ("alpha", "1"),
        ("beta", "2"),
    )
    assert chunks[0].parent_index is None


def test_unsupported_extension_raises_before_any_conversion(tmp_path: Path) -> None:
    path = tmp_path / "notes.rst"
    path.write_text("Title\n=====\n")
    try:
        docling_adapter.detect_format(path)
    except docling_adapter.UnsupportedDocumentFormatError:
        pass
    else:
        raise AssertionError("expected UnsupportedDocumentFormatError")


def test_corrupt_docx_raises_document_conversion_error() -> None:
    path = FIXTURES / "corrupt.docx"
    try:
        docling_adapter.convert(path)
    except docling_adapter.DocumentConversionError:
        pass
    else:
        raise AssertionError("expected DocumentConversionError")


def test_page_numbers_come_from_native_prov_not_a_marker() -> None:
    """Phase 1B: page numbers are read straight off each item's real
    ``prov`` (via ``normalizer._page_range``) -- no page-break marker or
    override involved. A synthetic multi-page ``DoclingDocument`` built
    with ``prov`` set directly, exactly the shape Docling's real PDF
    pipeline itself produces (see ``docling_adapter``'s module docstring)
    -- no real PDF or ML model needed to prove this.
    """
    doc = DoclingDocument(name="synthetic")
    doc.add_title("Doc Title", prov=_prov(1))
    doc.add_text(DocItemLabel.TEXT, "Page one text.", prov=_prov(1))
    doc.add_heading("Section Two", level=1, prov=_prov(2))
    doc.add_text(DocItemLabel.TEXT, "Page two text.", prov=_prov(2))
    doc.add_text(DocItemLabel.TEXT, "Page three text.", prov=_prov(3))

    normalized = normalizer.normalize(doc, DocumentFormat.PDF)

    assert [u.text for u in normalized.units] == [
        "Doc Title",
        "Page one text.",
        "Section Two",
        "Page two text.",
        "Page three text.",
    ]
    by_text = {u.text: u for u in normalized.units}
    assert by_text["Doc Title"].page_start == 1
    assert by_text["Doc Title"].page_end == 1
    assert by_text["Page one text."].page_start == 1
    assert by_text["Section Two"].page_start == 2
    assert by_text["Page two text."].page_start == 2
    assert by_text["Page three text."].page_start == 3
