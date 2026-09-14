"""Thin wrapper around Docling's Python API.

Docling's PDF pipeline downloads layout/table-structure model weights from
Hugging Face on first use -- real, but a CI/sandbox reliability risk (see
the ``docling_pdf`` pytest marker registered in ``pyproject.toml``). Every
other format handled here (DOCX, PPTX, XLSX, HTML, Markdown, TXT, EML) is
converted by Docling's rule-based backends and never touches that download
path, so only PDF conversion needs to be gated behind that marker in
tests.

PDF gets one extra step the other formats don't: Docling's real PDF-layout
``DoclingDocument`` is expensive to produce (that layout/table-structure
model), so it is cached -- keyed by content hash, in
``document_conversion_cache`` (see ``storage/schema.py``'s
``KNOWLEDGE_DB_V6``/``KNOWLEDGE_DB_V10``) -- as its own serialized JSON, via
its pydantic model's native ``model_dump_json()``/``model_validate_json()``
round-trip (``DoclingDocument`` is itself a pydantic model in this
project's pinned ``docling-core`` version). A cache hit deserializes that
JSON straight back into the *same* ``DoclingDocument`` Docling's PDF
pipeline produced -- headings, tables, text items, and every item's real
``prov`` page provenance -- without touching the model at all. This is
callers' only source of a PDF's ``DoclingDocument``: unlike the Phase 3
design this replaces, there is no second, reparsed document with
different properties than the first.

Earlier (Phase 3 through the search-quality improvement plan's Phase 1A)
this cached a Markdown *export* of that document instead, and reparsed the
Markdown through Docling's separate Markdown backend into a second
``DoclingDocument`` -- the one actually normalized/chunked/indexed. That
reparsed document carried no page provenance of its own (a plain
Markdown-backend parse attaches no ``prov`` to anything, and its
``num_pages()`` was always 0), so a page-break marker string embedded in
the Markdown export stood in for real provenance, and callers
reconstructed page numbers by counting marker crossings while walking the
document. The round-trip existed to let a cache hit skip the PDF pipeline
by feeding *something* back through Docling's (cheap, rule-based) Markdown
backend rather than needing to reconstruct a full ``DoclingDocument`` from
scratch. Caching the native document's own JSON serialization instead
gets the same "skip the expensive pipeline on a cache hit" property more
directly -- deserializing pydantic JSON is at least as cheap as reparsing
Markdown, doesn't require a second Docling backend at all, and needs no
marker/reconstruction hack because the document that comes back already
has every item's real ``prov``. Nothing else the Markdown round-trip
happened to provide (its own text normalization, whitespace collapsing,
etc.) is lost: those are round-tripped losslessly by the pydantic model
itself, whereas the Markdown export was Docling's own *lossy* rendering of
the very same document -- serializing the document directly is strictly
more faithful, not less.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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


# Identifies the cache row's ``serialized_document`` encoding -- currently
# only one value exists, but this is a stored (not just implied-by-
# cache_version) column so a future second encoding could be introduced
# and distinguished without another cache_version bump forcing every
# existing row to be treated as stale.
_SERIALIZATION_FORMAT = "docling_document/json"

# Bump whenever the meaning of ``serialized_document`` changes -- the
# encoding above, or which Docling version's ``DoclingDocument`` shape it
# assumes -- so a cache row written under the old meaning is treated as a
# miss rather than reused. See document_conversion_cache_repo.get's
# cache_version handling. Bumped for Phase 1B (search-quality improvement
# plan): this project's documented migration strategy is to never attempt
# to convert an old cache row forward -- a version bump alone makes every
# row written before it (including every Markdown-keyed row from before
# Phase 1B) a guaranteed miss, so it is simply regenerated, natively, the
# next time its PDF is indexed.
_CACHE_VERSION = 2


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

    Just ``document`` -- every format, PDF included, hands back the real
    ``DoclingDocument`` Docling itself produced (from the PDF pipeline, or
    from a cache hit's deserialization of a prior run of it), so callers
    read page numbers and provenance the same way regardless of format:
    ``document.num_pages()`` and each item's own ``prov``. Kept as a
    dataclass (rather than ``convert()`` returning a bare
    ``DoclingDocument``) as a stable extension point for future per-
    conversion metadata, mirroring its shape before Phase 1B removed the
    PDF-only ``page_count``/``page_break_marker`` fields it used to carry.
    """

    document: DoclingDocument


def _run_conversion(converter: DocumentConverter, path: Path, source: str) -> DoclingDocument:
    """Runs ``converter`` and returns its resulting document, or raises
    ``DocumentConversionError`` -- shared by the PDF and non-PDF
    conversion paths below.
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


def _deserialize_cached_document(
    cached: document_conversion_cache_repo.CachedConversion,
) -> DoclingDocument | None:
    """Reconstructs the cached ``DoclingDocument`` from
    ``cached.serialized_document``, or ``None`` if it can't be -- treated
    exactly like a cache miss by ``_convert_pdf``, which falls back to
    reconverting. This is deliberately defensive beyond what
    ``cache_version`` alone already guards: a row can only have been
    written by *this* code (nothing else populates
    ``serialized_document``), but an in-place ``pip install`` upgrade of
    ``docling-core`` between the write and this read could still change
    ``DoclingDocument``'s pydantic shape without anyone having bumped
    ``_CACHE_VERSION`` for it, and a hand-edited or truncated row is
    always possible. Falling back to a real reconversion is exactly as
    correct as a cache miss and costs nothing beyond that miss's own
    price, so there is no reason to let a deserialization failure here
    propagate as a hard error.
    """
    from docling_core.types.doc.document import DoclingDocument

    if cached.serialization_format != _SERIALIZATION_FORMAT:
        return None
    try:
        return DoclingDocument.model_validate_json(cached.serialized_document)
    except Exception:  # noqa: BLE001 - any deserialization failure degrades to a cache miss
        return None


def _convert_pdf(path: Path, conn: sqlite3.Connection | None) -> ConversionResult:
    """PDF's extra step: Docling's real PDF-layout pipeline is expensive
    (a real layout/table-structure ML model), so its output -- the native
    ``DoclingDocument`` -- is cached by content hash and deserialized
    straight back on a hit, never touching the pipeline again. See this
    module's docstring for the full rationale.
    """
    content_hash = hash_file(path)
    cached = (
        document_conversion_cache_repo.get(conn, content_hash, cache_version=_CACHE_VERSION)
        if conn is not None
        else None
    )
    if cached is not None:
        document = _deserialize_cached_document(cached)
        if document is not None:
            return ConversionResult(document=document)

    document = _run_conversion(_get_converter(), path, str(path))
    if conn is not None:
        import docling

        with transaction(conn):
            document_conversion_cache_repo.put(
                conn,
                document_conversion_cache_repo.CachedConversion(
                    content_hash=content_hash,
                    serialized_document=document.model_dump_json(),
                    serialization_format=_SERIALIZATION_FORMAT,
                    page_count=document.num_pages() or None,
                    parser_version=docling.__version__,
                ),
                cache_version=_CACHE_VERSION,
                created_at=datetime.now(UTC).isoformat(),
            )
    return ConversionResult(document=document)


def convert(path: Path, *, conn: sqlite3.Connection | None = None) -> ConversionResult:
    """Converts ``path`` (already confirmed supported by ``detect_format``)
    to a Docling ``DoclingDocument``, wrapped in a ``ConversionResult``, or
    raises ``DocumentConversionError``.

    ``conn``, when given, is the caller's already-open per-project
    ``knowledge.db`` connection -- used only for PDF's conversion cache
    (``document_conversion_cache``). Every other format ignores it
    entirely and converts exactly as it always has.
    """
    if detect_format(path) is DocumentFormat.PDF:
        return _convert_pdf(path, conn)
    document = _run_conversion(_get_converter(), path, str(path))
    return ConversionResult(document=document)
