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

Search Quality Improvement Plan, Phase 5: ``documents.ocr`` (``"off"`` |
``"auto"`` | ``"always"``) wires up Docling's real OCR pipeline, which
Phase 3 deliberately left unimplemented (``pdf_options.do_ocr`` was always
``False``). ``"off"`` is exactly Phase 3's original, only behavior --
unchanged. ``"auto"`` runs the plain pipeline first (``_get_converter``,
cached as before) and only re-runs conversion with OCR enabled
(``_get_ocr_converter``, a second, independent singleton) when that plain
result looks too textless to be real body content -- see
``_should_ocr``/``normalizer._is_low_text_density``. ``"always"`` skips
that detection pass and runs the OCR pipeline unconditionally, since a
caller who already wants OCR gains nothing from paying for two pipeline
runs. Either way, the result is cached in the same
``document_conversion_cache`` table the plain pipeline already used,
keyed by content hash *and* whether OCR was actually applied to produce
that row (see ``document_conversion_cache_repo``'s ``ocr_used`` column) --
so a page cached from a plain conversion is never handed back for a
request that actually needed OCR, and vice versa, while "auto" that
doesn't trigger OCR still reuses (and populates) the exact same cache
entry "off" would.

This project's pinned Docling range does not add a hard new dependency
for this: plain ``docling`` already transitively pulls in RapidOCR (see
``pyproject.toml``'s dependency comment) as of the version this was
written against. Regardless, ``_run_ocr_conversion`` degrades gracefully
-- returns ``None``, logs a warning, and callers fall back to the plain
conversion -- whenever OCR genuinely can't run in a given environment
(Docling's own ``OcrAutoOptions`` already does this silently for "no
engine installed"; ``_run_ocr_conversion`` additionally covers any other
OCR-specific failure, e.g. a blocked model-weight download) so a missing
or broken OCR engine never fails the whole file.

Search Quality Improvement Plan, Phase 10: five more formats join
``EXTENSION_TO_FORMAT`` -- CSV, ODT/ODS/ODP, EPUB -- all genuinely
converted by Docling's own rule-based backends, verified by reading the
*installed* Docling version's actual backend modules rather than
assuming the plan's tier list holds:

- CSV/EPUB need nothing new (``docling.backend.csv_backend`` is stdlib
  ``csv``; ``docling.backend.epub_backend`` is stdlib ``zipfile`` +
  the already-required ``defusedxml``).
- ODT/ODS/ODP need ``odfdo`` (``docling.backend.opendocument_backend``
  imports it behind the same guarded-import-degrades-to-ImportError
  pattern ``mailparser``/``python-oxmsg`` use for EML/MSG) -- unlike
  RapidOCR/mailparser/oxmsg, this is genuinely *not* pulled in
  transitively by plain ``docling`` at this pinned range's current
  resolution, so it is this phase's one new, explicit dependency (see
  ``pyproject.toml``). It earns the exception to this project's
  "avoid heavy new dependencies" policy the OCR/VLM/ASR/XBRL extras and
  LibreOffice (below) don't: pure Python, its only import is the
  already-required ``lxml``, no ML weights, no subprocess, no system
  binary -- the same weight class as the dev-only ``python-docx``/
  ``python-pptx``/``openpyxl`` already used to generate this project's
  own DOCX/PPTX/XLSX fixtures, just needed at runtime instead of only
  for tests.

Three formats this phase's plan expected to be free turned out, on
reading the installed Docling version's actual backend source, to
require a working LibreOffice (``soffice``) subprocess with **no**
pure-Python fallback: legacy ``.doc``/``.ppt``/``.xls`` (already true
before this phase, per ``UnsupportedDocumentFormatError``'s docstring)
and, newly discovered, ``.rtf`` too --
``docling.backend.msword_backend.MsWordDocumentBackend.__init__``
unconditionally calls ``convert_to_modern_format`` (which shells out to
``soffice --convert-to``) for ``InputFormat.DOC`` *and*
``InputFormat.RTF`` alike, and ``docling.backend.msexcel_backend``/
``mspowerpoint_backend`` do the same for XLS/PPT. This project's
explicit policy is to never add a LibreOffice dependency (see this
phase's plan and ``UnsupportedDocumentFormatError``'s docstring), so
none of these four are wired into ``EXTENSION_TO_FORMAT`` -- they stay
exactly as before, detected as ``FileKind.DOCUMENT`` but converted by
nothing, indexed with no derived content. This was also confirmed
empirically, not just by reading source: even in a development sandbox
where a ``soffice`` binary happened to be installed, headless
``--convert-to`` invocations failed outright (on *any* input, not just
RTF), underlining exactly why this project treats LibreOffice as an
unreliable environment dependency to avoid rather than one more format
to wire up.

Outlook ``.msg`` is deferred for a different, non-technical reason: both
``mail-parser`` and ``python-oxmsg`` (Docling's own guarded-import deps
for ``InputFormat.EMAIL``, which already covers ``.eml``) are already
transitively installed, so ``.msg`` would need zero new code or
dependencies. But a ``.msg`` is a real Outlook-authored OLE2/CFBF
(MAPI) binary structure -- unlike every other fixture in this module
(including the hand-assembled minimal PDFs in
``tests/fixtures/documents/generate_fixtures.py``, which only need to
be well-formed enough for a *parser*, not authored by real Office
software), there is no writer library available here and no network
access in this environment to fetch a genuine sample, so a hand-rolled
``.msg`` risks either failing to parse (proving nothing) or silently
round-tripping empty text -- exactly what this plan's acceptance
criteria forbids claiming as "supported". Left for a future phase with
either a real sample fixture or a ``.msg``-writing dependency.

Raw images (``.png``/``.jpg``/``.jpeg``/``.tif``/``.tiff``,
``InputFormat.IMAGE``) are genuinely convertible, confirmed end-to-end
against real extracted OCR text -- but only *with* OCR: an image has no
embedded text layer, so unlike PDF there is no cheap non-OCR pass to
try first. Docling's own default ``ImageFormatOption`` already sets
``do_ocr=True`` (distinct from this module's PDF ``FormatOption``,
which explicitly forces ``do_ocr=False``), so simply adding
``InputFormat.IMAGE`` to ``_get_converter()``'s ``allowed_formats``
(already built generically from ``_format_to_input_format()``, no
image-specific converter needed) is enough to make ``convert()`` OCR
every image it's asked to convert -- no separate image pipeline to
maintain alongside PDF's OCR one. Because that means *every* image
conversion now pays for a real OCR pass (the same RapidOCR engine
Phase 5 confirmed ships transitively, just invoked here without PDF's
"auto" text-density pre-check to skip it when unneeded), this is gated
behind ``documents.image_ocr`` (default off) rather than being
unconditionally on like CSV/ODT/ODS/ODP/EPUB -- see
``documents/pipeline.py`` for where that gate is applied, and
``core/config.py``'s ``DocumentsConfig.image_ocr`` docstring.
"""

from __future__ import annotations

import logging
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
from ragpilot.telemetry.logging import get_logger, log_event

_logger = get_logger("docling_adapter")

# Search Quality Improvement Plan, Phase 12: this module's own axis of the
# document pipeline's reuse identity (``indexing/incremental.
# VersionStamp``), stamped onto ``files.parser_version`` -- an existing
# column (``KNOWLEDGE_DB_V1``) this phase starts actually writing
# meaningfully for the first time; see ``storage/repositories/
# files_repo.py``'s ``mark_indexed``.
#
# Deliberately NOT aliased to ``_CACHE_VERSION`` below, even though both
# currently guard "did the way this module produces a document change":
# ``files.parser_version`` has defaulted to ``"1"`` for every file row
# ever written, including every document already indexed before this
# phase shipped, while ``_CACHE_VERSION`` is already at ``2`` (bumped for
# Phase 1B's cache-format change, unrelated to this). Starting
# ``PARSER_VERSION`` at ``"1"`` -- matching that pre-existing default --
# means adding this tracking is a true no-op for already-indexed content;
# aliasing it to ``_CACHE_VERSION`` instead would have made every such
# document look stale and force a full reindex the moment this phase
# shipped, exactly what it must not do. The two may still move together
# in practice (a change big enough to bump one is often big enough to
# bump the other), but nothing enforces that coupling -- bump each only
# when its own thing actually changes: ``_CACHE_VERSION`` when
# ``serialized_document``'s meaning changes, ``PARSER_VERSION`` when this
# module's own conversion/normalization output could differ for the same
# input.
PARSER_VERSION = "1"

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

# Phase 3's original extensions, plus Phase 10's genuine additions (see
# this module's docstring). Anything else that ``sources.detector`` still
# classifies as ``FileKind.DOCUMENT`` -- legacy .doc/.ppt/.xls, .rtf,
# .rst, and Outlook .msg -- is deliberately out of scope; see
# ``UnsupportedDocumentFormatError`` and this module's docstring for why
# each one specifically.
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
    ".csv": DocumentFormat.CSV,
    ".odt": DocumentFormat.ODT,
    ".ods": DocumentFormat.ODS,
    ".odp": DocumentFormat.ODP,
    ".epub": DocumentFormat.EPUB,
    ".png": DocumentFormat.IMAGE,
    ".jpg": DocumentFormat.IMAGE,
    ".jpeg": DocumentFormat.IMAGE,
    ".tif": DocumentFormat.IMAGE,
    ".tiff": DocumentFormat.IMAGE,
}

# Real extraction from ``DocumentFormat.IMAGE`` requires OCR (see this
# module's docstring) -- ``documents/pipeline.py`` checks membership here
# against ``config.documents.image_ocr`` before ever calling ``convert()``
# for an image, so a project that hasn't opted in gets the same "detected,
# no derived content" fallback as a genuinely unsupported extension rather
# than an unexpectedly slow/OCR-heavy index run.
FORMATS_REQUIRING_IMAGE_OCR: frozenset[DocumentFormat] = frozenset({DocumentFormat.IMAGE})

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
            DocumentFormat.CSV: InputFormat.CSV,
            DocumentFormat.ODT: InputFormat.ODT,
            DocumentFormat.ODS: InputFormat.ODS,
            DocumentFormat.ODP: InputFormat.ODP,
            DocumentFormat.EPUB: InputFormat.EPUB,
            # Docling's own default ``ImageFormatOption`` already sets
            # ``do_ocr=True`` -- see this module's docstring for why no
            # separate image converter/pipeline is needed here.
            DocumentFormat.IMAGE: InputFormat.IMAGE,
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

# ``document_conversion_cache_repo``'s ``ocr_used`` cache-key column: which
# of the two possible pipeline outputs a given row holds for its
# content_hash. Not the 3-value ``documents.ocr`` config setting itself --
# "auto" resolves to one of these two at conversion time (see
# ``_convert_pdf``), so a cache row only ever needs to record which
# pipeline *actually ran*, not which mode asked for it. This is what makes
# "auto" (when it doesn't trigger OCR) transparently share "off"'s cache
# entry, and "auto" (when it does trigger OCR) transparently share
# "always"'s.
_OCR_NOT_APPLIED = "off"
_OCR_APPLIED = "on"


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
            f"'{path.suffix or '<none>'}' (supports PDF, DOCX, PPTX, "
            "XLSX, HTML, Markdown, TXT, EML, CSV, ODT, ODS, ODP, EPUB, "
            "and images with documents.image_ocr enabled -- see "
            "docling_adapter.py's module docstring for what's "
            "deliberately still out of scope: legacy .doc/.ppt/.xls, "
            ".rtf, and Outlook .msg)"
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
        # The plain, always-available pipeline: OCR off, so this converter
        # never needs an OCR engine's own model weights on top of the
        # layout/table-structure ones. Every ``documents.ocr`` setting
        # ("off", and "auto" before/unless it triggers) uses this same
        # singleton; OCR itself is a second, independent converter (see
        # ``_get_ocr_converter``) rather than a flag flipped on this one,
        # so an OCR request elsewhere can never change what this pipeline
        # does for every other caller.
        pdf_options.do_ocr = False
        pdf_options.do_table_structure = True
        _converter = DocumentConverter(
            allowed_formats=list(input_formats.values()),
            format_options={
                input_formats[DocumentFormat.PDF]: PdfFormatOption(pipeline_options=pdf_options)
            },
        )
    return _converter


_ocr_converter: DocumentConverter | None = None


def _get_ocr_converter() -> DocumentConverter:
    """The OCR-enabled counterpart to ``_get_converter``, built and cached
    the same way. Only ``documents.ocr``'s "auto" (once triggered) and
    "always" modes ever call this -- see ``_convert_pdf`` -- so a run that
    never needs OCR never pays to construct it.

    Deliberately leaves ``ocr_options`` at its Docling default
    (``OcrAutoOptions``) rather than pinning a specific engine: Docling's
    own auto-selection already tries whatever OCR engine is actually
    installed (RapidOCR, EasyOCR, ocrmac, ...) and, if none is, logs a
    warning and runs with OCR effectively a no-op instead of raising --
    exactly the graceful degradation this module wants, provided for free.
    """
    global _ocr_converter
    if _ocr_converter is None:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        input_formats = _format_to_input_format()
        pdf_options = PdfPipelineOptions()
        pdf_options.do_ocr = True
        pdf_options.do_table_structure = True
        _ocr_converter = DocumentConverter(
            allowed_formats=list(input_formats.values()),
            format_options={
                input_formats[DocumentFormat.PDF]: PdfFormatOption(pipeline_options=pdf_options)
            },
        )
    return _ocr_converter


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


def _cached_document(
    conn: sqlite3.Connection | None, content_hash: str, *, ocr_used: str
) -> DoclingDocument | None:
    """Looks up ``document_conversion_cache`` for ``content_hash``'s row
    written under ``ocr_used`` (``_OCR_NOT_APPLIED``/``_OCR_APPLIED``),
    deserializing it -- or ``None`` on any kind of miss (no connection, no
    row, a stale ``cache_version``, or a deserialization failure -- see
    ``_deserialize_cached_document``). Shared by every ``_convert_pdf``
    branch so a cache lookup for either pipeline output looks the same.
    """
    if conn is None:
        return None
    cached = document_conversion_cache_repo.get(
        conn, content_hash, ocr_used=ocr_used, cache_version=_CACHE_VERSION
    )
    return _deserialize_cached_document(cached) if cached is not None else None


def _store_document(
    conn: sqlite3.Connection | None,
    content_hash: str,
    document: DoclingDocument,
    *,
    ocr_used: str,
) -> None:
    """Upserts ``document`` into ``document_conversion_cache`` under
    ``content_hash``/``ocr_used`` -- a no-op when ``conn`` is ``None``
    (callers that convert without a cache at all, e.g. the ``docling_pdf``
    golden test's bare ``convert(path)``).
    """
    if conn is None:
        return
    import docling

    with transaction(conn):
        document_conversion_cache_repo.put(
            conn,
            document_conversion_cache_repo.CachedConversion(
                content_hash=content_hash,
                ocr_used=ocr_used,
                serialized_document=document.model_dump_json(),
                serialization_format=_SERIALIZATION_FORMAT,
                page_count=document.num_pages() or None,
                parser_version=docling.__version__,
            ),
            cache_version=_CACHE_VERSION,
            created_at=datetime.now(UTC).isoformat(),
        )


def _should_ocr(document: DoclingDocument) -> bool:
    """True when ``document`` -- the plain pipeline's own output -- looks
    too textless to be real body content, i.e. OCR would likely help. Runs
    the exact same check ``normalizer.normalize``'s ``is_scanned`` flag
    does (``normalizer._is_low_text_density``), so "does this look
    scanned" has one definition, not two independently-tuned ones -- see
    both modules' docstrings.
    """
    from ragpilot.documents import normalizer

    return normalizer.normalize(document, DocumentFormat.PDF).is_scanned


def _run_ocr_conversion(path: Path) -> DoclingDocument | None:
    """Runs the OCR-enabled pipeline (``_get_ocr_converter``), or returns
    ``None`` if OCR genuinely couldn't run here. Docling's own
    ``OcrAutoOptions`` already degrades silently (logs a warning, no OCR
    applied) when no OCR engine is installed at all -- but this is
    deliberately broader than catching just that, covering any other
    OCR-specific failure a real environment can hit (a blocked/partial
    model-weight download, an unexpected Docling exception), exactly like
    ``_run_conversion``'s own ``except Exception`` for the plain pipeline.
    Callers treat ``None`` exactly like "OCR wouldn't have helped": keep
    (or fall back to) the plain, non-OCR conversion rather than failing
    the whole file over an OCR-only problem.
    """
    try:
        return _run_conversion(_get_ocr_converter(), path, str(path))
    except Exception as exc:  # noqa: BLE001 - OCR is best-effort, see docstring
        log_event(
            _logger,
            "ocr_conversion_failed",
            level=logging.WARNING,
            path=str(path),
            error=str(exc),
        )
        return None


def _convert_pdf(
    path: Path, conn: sqlite3.Connection | None, ocr_mode: str = "off"
) -> ConversionResult:
    """PDF's extra step: Docling's real PDF-layout pipeline is expensive
    (a real layout/table-structure ML model), so its output -- the native
    ``DoclingDocument`` -- is cached by content hash (and, since Phase 5,
    whether OCR was applied) and deserialized straight back on a hit,
    never touching the pipeline again. See this module's docstring for
    the full rationale, including ``ocr_mode``'s "off"/"auto"/"always"
    behavior.
    """
    content_hash = hash_file(path)

    if ocr_mode == "always":
        document = _cached_document(conn, content_hash, ocr_used=_OCR_APPLIED)
        if document is None:
            document = _run_ocr_conversion(path)
            if document is not None:
                _store_document(conn, content_hash, document, ocr_used=_OCR_APPLIED)
        if document is None:
            # OCR genuinely unavailable (see _run_ocr_conversion) -- "always"
            # still has to return something, so this is the one place OCR
            # being requested falls back to the plain pipeline instead of
            # never running it.
            document = _cached_document(conn, content_hash, ocr_used=_OCR_NOT_APPLIED)
            if document is None:
                document = _run_conversion(_get_converter(), path, str(path))
                _store_document(conn, content_hash, document, ocr_used=_OCR_NOT_APPLIED)
        return ConversionResult(document=document)

    # "off", or "auto" before its detection pass below: always the plain,
    # non-OCR pipeline -- exactly Phase 3's only path, and "off"'s own
    # cache entry (which "auto" transparently reuses/populates too when it
    # doesn't end up triggering OCR).
    document = _cached_document(conn, content_hash, ocr_used=_OCR_NOT_APPLIED)
    if document is None:
        document = _run_conversion(_get_converter(), path, str(path))
        _store_document(conn, content_hash, document, ocr_used=_OCR_NOT_APPLIED)

    if ocr_mode == "auto" and _should_ocr(document):
        ocr_document = _cached_document(conn, content_hash, ocr_used=_OCR_APPLIED)
        if ocr_document is None:
            ocr_document = _run_ocr_conversion(path)
            if ocr_document is not None:
                _store_document(conn, content_hash, ocr_document, ocr_used=_OCR_APPLIED)
        if ocr_document is not None:
            document = ocr_document
        # else: OCR unavailable/failed -- keep the plain document already
        # produced above; _run_ocr_conversion already logged why.

    return ConversionResult(document=document)


def convert(
    path: Path, *, conn: sqlite3.Connection | None = None, ocr_mode: str = "off"
) -> ConversionResult:
    """Converts ``path`` (already confirmed supported by ``detect_format``)
    to a Docling ``DoclingDocument``, wrapped in a ``ConversionResult``, or
    raises ``DocumentConversionError``.

    ``conn``, when given, is the caller's already-open per-project
    ``knowledge.db`` connection -- used only for PDF's conversion cache
    (``document_conversion_cache``). Every other format ignores it
    entirely and converts exactly as it always has.

    ``ocr_mode`` is ``documents.ocr``'s effective value ("off" | "auto" |
    "always"), meaningful for PDF only -- every other format ignores it,
    since OCR is a PDF-pipeline concept. Defaults to "off" (not this
    project's real "auto" default) so every existing caller that doesn't
    pass it -- direct tests included -- keeps Phase 3's exact, unchanged
    behavior; the real default is applied by ``indexing/coordinator.py``
    threading ``config.documents.ocr`` through ``ProcessorContext``, the
    same pattern ``chunking``/``max_document_pages`` already use. Any
    value other than "auto"/"always" (including "off", and defensively
    anything unrecognized) takes the "off" path -- config itself already
    restricts ``documents.ocr`` to these three values (see
    ``core.config.DocumentsConfig``), so this is a last-resort safety net,
    not the primary validation.
    """
    if detect_format(path) is DocumentFormat.PDF:
        return _convert_pdf(path, conn, ocr_mode)
    document = _run_conversion(_get_converter(), path, str(path))
    return ConversionResult(document=document)
