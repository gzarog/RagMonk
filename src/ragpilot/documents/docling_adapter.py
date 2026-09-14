"""Thin wrapper around Docling's Python API.

Docling's PDF pipeline downloads layout/table-structure model weights from
Hugging Face on first use -- real, but a CI/sandbox reliability risk (see
the ``docling_pdf`` pytest marker registered in ``pyproject.toml``). Every
other format handled here (DOCX, PPTX, XLSX, HTML, Markdown, TXT, EML) is
converted by Docling's rule-based backends and never touches that download
path, so only PDF conversion needs to be gated behind that marker in
tests.

PDF gets one extra step the other formats don't: rather than normalizing
Docling's PDF-layout ``DoclingDocument`` directly, ``convert()`` exports it
to Markdown text, caches that text (keyed by content hash, in
``document_conversion_cache`` -- see ``storage/schema.py``'s
``KNOWLEDGE_DB_V6``), and reparses the Markdown through Docling's own
Markdown backend into a *second* ``DoclingDocument`` -- and it is that
reparsed document callers actually normalize/chunk/index. This means the
PDF layout/table-structure model only ever runs once per distinct PDF
content, no matter how many times the file is re-indexed: a cache hit
reparses cached Markdown without touching the model at all. The reparsed
document carries no page provenance of its own (a plain Markdown-backend
parse attaches no ``prov`` to anything, and its ``num_pages()`` is always
0), so ``convert()`` returns the real page count and a page-break marker
string alongside it -- see ``ConversionResult`` -- for
``normalizer.normalize`` to reconstruct page numbers from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from ragpilot.core.errors import RagpilotError
from ragpilot.core.models import DocumentFormat
from ragpilot.sources.fingerprint import hash_file
from ragpilot.storage.repositories import document_conversion_cache_repo
from ragpilot.storage.sqlite import transaction

# CLI performance improvement plan, Phase 2: Docling (which itself pulls in
# torch for its layout/table-structure models) must never load just from
# `import ragpilot.documents.docling_adapter` -- only when a function here
# that actually needs it runs. Safe under annotations only (this module has
# `from __future__ import annotations`, so every annotation below is a
# string at runtime); every *runtime* use imports locally instead.
if TYPE_CHECKING:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc.document import DoclingDocument
    from docling_core.types.io import DocumentStream

# Phase 3's supported extensions. Anything else that ``sources.detector``
# still classifies as ``FileKind.DOCUMENT`` (legacy .doc/.ppt/.xls,
# OpenDocument, .rtf, .csv, .rst) is deliberately out of scope -- see
# ``UnsupportedDocumentFormatError``.
EXTENSION_TO_FORMAT: dict[str, DocumentFormat] = {
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".pptx": DocumentFormat.PPTX,
    ".xlsx": DocumentFormat.XLSX,
    ".html": DocumentFormat.HTML,
    ".htm": DocumentFormat.HTML,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".txt": DocumentFormat.TXT,
    ".eml": DocumentFormat.EML,
}

_format_to_input_format_cache: dict[DocumentFormat, InputFormat] | None = None


def _format_to_input_format() -> dict[DocumentFormat, InputFormat]:
    """Built lazily and cached: its values are Docling's own ``InputFormat``
    enum members, so building this at import time would itself force
    Docling to load.
    """
    global _format_to_input_format_cache
    if _format_to_input_format_cache is None:
        from docling.datamodel.base_models import InputFormat

        _format_to_input_format_cache = {
            DocumentFormat.PDF: InputFormat.PDF,
            DocumentFormat.DOCX: InputFormat.DOCX,
            DocumentFormat.PPTX: InputFormat.PPTX,
            DocumentFormat.XLSX: InputFormat.XLSX,
            DocumentFormat.HTML: InputFormat.HTML,
            # Docling itself has no distinct "plain text" InputFormat -- .md
            # and .txt both route to InputFormat.MD (see
            # docling.datamodel.base_models.FormatToExtensions), and plain
            # text is valid (trivial) Markdown.
            DocumentFormat.MARKDOWN: InputFormat.MD,
            DocumentFormat.TXT: InputFormat.MD,
            DocumentFormat.EML: InputFormat.EMAIL,
        }
    return _format_to_input_format_cache


# Plain text, no Markdown/HTML special syntax: an HTML-comment-style
# placeholder (e.g. "<!--PAGEBREAK-->") is silently swallowed by Docling's
# Markdown parser and produces no item at all when reparsed, so it cannot
# be recovered by ``normalizer.normalize``. Wrapped in U+2063 INVISIBLE
# SEPARATOR on both sides to minimize accidental collision with real
# document text while still round-tripping as an exact, distinct
# ``TextItem.text`` value through the reparse.
PAGE_BREAK_MARKER = "⁣RAGPILOT-PAGE-BREAK⁣"

# Bump if PAGE_BREAK_MARKER or export_to_markdown's options ever change --
# see document_conversion_cache_repo.get's cache_version handling for why
# a stale-version cache row must be treated as a miss rather than reused.
_CACHE_VERSION = 1


class UnsupportedDocumentFormatError(RagpilotError):
    """A ``FileKind.DOCUMENT`` file whose extension is outside Phase 3's
    supported format list. The processor catches this specifically and
    indexes the file without derived document content instead of failing
    the run, mirroring ``code/processor.py``'s "recognized extension, no
    grammar wired up yet" fallback.
    """


class DocumentConversionError(RagpilotError):
    """Docling itself failed to convert an otherwise-supported file
    (corrupt, password-protected, malformed, ...). Left to propagate to
    the coordinator's existing per-file isolation/retry handling, exactly
    like ``code.processor.CodeParseError``.
    """


def detect_format(path: Path) -> DocumentFormat:
    fmt = EXTENSION_TO_FORMAT.get(path.suffix.lower())
    if fmt is None:
        raise UnsupportedDocumentFormatError(
            f"{path}: unsupported document extension "
            f"'{path.suffix or '<none>'}' (Phase 3 supports PDF, DOCX, "
            "PPTX, XLSX, HTML, Markdown, TXT, EML)"
        )
    return fmt


_converter: DocumentConverter | None = None


def _get_converter() -> DocumentConverter:
    """Module-level singleton: constructing a ``DocumentConverter`` is what
    loads the (lazy) layout/table-structure model handles, so building one
    per file would repeat that cost for every PDF in a run.
    """
    global _converter
    if _converter is None:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        input_formats = _format_to_input_format()
        pdf_options = PdfPipelineOptions()
        # OCR is explicitly out of scope for Phase 3 (config.documents.ocr
        # is read only for the future -- see docs/CHANGELOG). Turning it
        # off also means PDF conversion never needs an OCR engine's own
        # model weights on top of the layout/table-structure ones.
        pdf_options.do_ocr = False
        pdf_options.do_table_structure = True
        _converter = DocumentConverter(
            allowed_formats=list(input_formats.values()),
            format_options={
                input_formats[DocumentFormat.PDF]: PdfFormatOption(pipeline_options=pdf_options)
            },
        )
    return _converter


def pdf_page_count(path: Path) -> int:
    """Page count via ``pypdfium2`` alone -- no layout/table-structure
    model weights are loaded for this. Lets a ``documents.max_pages``
    check reject an oversized PDF before ``convert()`` would trigger
    Docling's model download at all.
    """
    import pypdfium2 as pdfium

    handle = pdfium.PdfDocument(str(path))
    try:
        return len(handle)
    finally:
        handle.close()


@dataclass(frozen=True)
class ConversionResult:
    """``convert()``'s return type.

    For every non-PDF format, ``page_count``/``page_break_marker`` are
    both ``None`` -- callers fall back to ``document``'s own
    ``num_pages()``/``item.prov``, exactly as before this type existed.

    For PDF, ``document`` is the *reparsed-from-Markdown* document, not
    Docling's PDF-layout document -- it carries no ``prov`` on any item
    and its own ``num_pages()`` is always 0, so ``page_count`` carries the
    real PDF page count (via ``pdf_page_count``) instead, and
    ``page_break_marker`` is ``PAGE_BREAK_MARKER``: the literal text
    callers must special-case when walking ``document``'s items to
    reconstruct page numbers (see ``normalizer.normalize``).
    """

    document: DoclingDocument
    page_count: int | None = None
    page_break_marker: str | None = None


def _run_conversion(
    converter: DocumentConverter, path: Path, source: str | DocumentStream
) -> DoclingDocument:
    """Runs ``converter`` and returns its resulting document, or raises
    ``DocumentConversionError`` -- shared by both the PDF and non-PDF
    conversion paths below, and by the Markdown reparse.
    """
    from docling.datamodel.base_models import ConversionStatus

    try:
        result = converter.convert(source)
    except Exception as exc:  # noqa: BLE001 - Docling's own exception types vary by backend
        raise DocumentConversionError(
            f"{path}: Docling failed to convert this file: {exc}"
        ) from exc
    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        raise DocumentConversionError(
            f"{path}: Docling conversion did not succeed (status={result.status})"
        )
    return result.document


_md_converter: DocumentConverter | None = None


def _get_md_converter() -> DocumentConverter:
    """Separate singleton from ``_get_converter()``'s PDF-pipeline-
    configured one: this one never touches PDF layout/table-structure
    models at all, it is Docling's plain, rule-based Markdown backend --
    the same one used for real ``.md`` files, just fed cached or
    freshly-exported PDF Markdown instead of a file on disk.
    """
    global _md_converter
    if _md_converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter

        _md_converter = DocumentConverter(allowed_formats=[InputFormat.MD])
    return _md_converter


def _reparse_markdown(path: Path, markdown_text: str) -> DoclingDocument:
    """Reparses ``markdown_text`` (Docling's own Markdown export of a PDF)
    back into a ``DoclingDocument`` via Docling's Markdown backend, in
    memory -- no temp file on disk. The stream must be named with an
    MD-recognized extension: Docling sniffs format from the name even
    when ``allowed_formats`` only contains one entry, so the resulting
    document's ``origin.filename`` ends up being this synthetic
    ``"<stem>.md"`` name rather than the real PDF's -- harmless, since
    ``metadata.extract_metadata``'s ``source_filename`` field is computed
    but never consumed anywhere else in this codebase.
    """
    from docling_core.types.io import DocumentStream

    stream = DocumentStream(name=f"{path.stem}.md", stream=BytesIO(markdown_text.encode("utf-8")))
    return _run_conversion(_get_md_converter(), path, stream)


def _convert_pdf(path: Path, conn: sqlite3.Connection | None) -> ConversionResult:
    """PDF's extra step: export Docling's real PDF-layout conversion to
    Markdown (cached by content hash so an unchanged PDF never re-runs
    the model), then reparse that Markdown into the document that
    actually gets normalized/chunked/indexed. See this module's
    docstring for the full rationale.
    """
    real_page_count = pdf_page_count(path)
    content_hash = hash_file(path)
    cached = (
        document_conversion_cache_repo.get(conn, content_hash, cache_version=_CACHE_VERSION)
        if conn is not None
        else None
    )
    if cached is not None:
        markdown = cached.markdown
    else:
        document = _run_conversion(_get_converter(), path, str(path))
        markdown = document.export_to_markdown(page_break_placeholder=PAGE_BREAK_MARKER)
        if conn is not None:
            with transaction(conn):
                document_conversion_cache_repo.put(
                    conn,
                    document_conversion_cache_repo.CachedConversion(
                        content_hash=content_hash, markdown=markdown, page_count=real_page_count
                    ),
                    cache_version=_CACHE_VERSION,
                    created_at=datetime.now(UTC).isoformat(),
                )
    reparsed = _reparse_markdown(path, markdown)
    return ConversionResult(
        document=reparsed, page_count=real_page_count, page_break_marker=PAGE_BREAK_MARKER
    )


def convert(path: Path, *, conn: sqlite3.Connection | None = None) -> ConversionResult:
    """Converts ``path`` (already confirmed supported by ``detect_format``)
    to a Docling ``DoclingDocument``, wrapped in a ``ConversionResult``, or
    raises ``DocumentConversionError``.

    ``conn``, when given, is the caller's already-open per-project
    ``knowledge.db`` connection -- used only for PDF's Markdown-export
    cache (``document_conversion_cache``). Every other format ignores it
    entirely and converts exactly as it always has.
    """
    if detect_format(path) is DocumentFormat.PDF:
        return _convert_pdf(path, conn)
    document = _run_conversion(_get_converter(), path, str(path))
    return ConversionResult(document=document)
