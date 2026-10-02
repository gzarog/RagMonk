"""Converts one decoded email attachment through the *existing* document
pipeline (EML attachment knowledge extraction V1, Phase P1).

No format-specific parsing lives here: an attachment is materialized to an
OS-created temporary directory under an internally generated name (only
the extension is carried over -- never the sender-supplied filename) and
then handed to exactly the same ``docling_adapter.convert`` ->
``normalizer.normalize`` -> ``metadata.extract_metadata`` ->
``chunker.chunk_document`` sequence a standalone file goes through, so an
attached PDF/DOCX/XLSX yields the same chunk semantics as the equivalent
file on disk and inherits OCR/chunking/embedding behavior for free.
"""

from __future__ import annotations

import dataclasses
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ragmonk.core.config import ChunkingConfig
from ragmonk.core.models import DocumentFormat
from ragmonk.documents import chunker, docling_adapter, normalizer
from ragmonk.documents.chunker import Chunk, ChunkingDiagnostics
from ragmonk.documents.email_attachments import (
    SKIP_IMAGE_OCR_DISABLED,
    SKIP_NESTED_MESSAGE,
    SKIP_PAGE_LIMIT,
    SKIP_UNSUPPORTED,
    AttachmentPart,
)
from ragmonk.documents.metadata import DocumentMetadata, extract_metadata

# Conservative MIME-type fallback, used only when the filename carries no
# supported extension. Ambiguous/generic types (``application/octet-
# stream``, ``application/zip``, ...) are deliberately absent.
_MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/epub+zip": ".epub",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
}


class AttachmentSkipped(Exception):  # noqa: N818 - control-flow signal, not an error
    """The attachment is intentionally not converted (unsupported type,
    image OCR off, page limit). Carries a ``SkippedAttachment`` reason code.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PreparedAttachment:
    """One converted attachment, ready to become a child ``Document``."""

    part_ordinal: int
    name: str
    content_type: str
    content_id: str | None
    size: int
    doc_format: DocumentFormat
    content_hash: str
    meta: DocumentMetadata
    chunks: list[Chunk] = field(default_factory=list)


def attachment_extension(part: AttachmentPart) -> str | None:
    """The supported extension for ``part`` -- from its sanitized filename
    first, then the conservative MIME fallback. ``None`` = unsupported.
    """
    if part.filename:
        suffix = PurePosixPath(part.filename).suffix.lower()
        if suffix in docling_adapter.EXTENSION_TO_FORMAT:
            return suffix
        if suffix:
            # An explicit, unsupported extension (.zip, .exe, .doc ...) wins
            # over whatever the MIME header claims.
            return None
    return _MIME_TO_EXTENSION.get(part.content_type.lower())


def attachment_format(part: AttachmentPart) -> DocumentFormat | None:
    ext = attachment_extension(part)
    return docling_adapter.EXTENSION_TO_FORMAT.get(ext) if ext else None


def prepare_attachment(
    part: AttachmentPart,
    *,
    cache_conn: object | None,
    ocr_mode: str,
    image_ocr: bool,
    chunking: ChunkingConfig | None,
    max_document_pages: int | None,
) -> PreparedAttachment:
    """Convert/normalize/chunk ``part``.

    Raises ``AttachmentSkipped`` for an intentionally unconverted part and
    lets any conversion exception propagate (the caller records it as an
    attachment failure without failing the parent email). The temporary
    materialization is always removed, on success and on error.
    """
    ext = attachment_extension(part)
    doc_format = docling_adapter.EXTENSION_TO_FORMAT.get(ext) if ext else None
    if ext is None or doc_format is None:
        raise AttachmentSkipped(SKIP_UNSUPPORTED, part.content_type)
    if doc_format is DocumentFormat.EML:
        # V1: attached emails are never recursed into.
        raise AttachmentSkipped(SKIP_NESTED_MESSAGE)
    if doc_format in docling_adapter.FORMATS_REQUIRING_IMAGE_OCR and not image_ocr:
        raise AttachmentSkipped(SKIP_IMAGE_OCR_DISABLED)

    content_hash = hashlib.sha256(part.payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ragmonk-attachment-") as tmp:
        path = Path(tmp) / f"attachment{ext}"
        path.write_bytes(part.payload)
        if doc_format is DocumentFormat.PDF and max_document_pages is not None:
            pages = docling_adapter.pdf_page_count(path)
            if pages > max_document_pages:
                raise AttachmentSkipped(SKIP_PAGE_LIMIT, f"{pages} pages")
        conversion = docling_adapter.convert(
            path,
            conn=cache_conn,  # type: ignore[arg-type]
            ocr_mode=ocr_mode,
            content_hash=content_hash,
        )
        normalized = normalizer.normalize(conversion.document, doc_format)
        meta = extract_metadata(conversion.document, normalized, doc_format, path)

    # The temp file's stem ("attachment") is meaningless as a title -- use
    # the attachment's own display name unless the document declared one.
    title = meta.title if meta.title and meta.title != path.stem else part.display_name
    meta = dataclasses.replace(meta, title=title, source_filename=part.display_name)
    chunks = chunker.chunk_document(
        normalized,
        config=chunking or ChunkingConfig(),
        doc_title=title,
        diagnostics=ChunkingDiagnostics(),
    )
    return PreparedAttachment(
        part_ordinal=part.ordinal,
        name=part.display_name,
        content_type=part.content_type,
        content_id=part.content_id,
        size=part.decoded_size,
        doc_format=doc_format,
        content_hash=content_hash,
        meta=meta,
        chunks=chunks,
    )
