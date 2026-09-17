"""Document-level metadata extraction: whatever Docling exposes, typed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docling_core.types.doc.document import DoclingDocument

from ragmonk.core.models import DocumentFormat
from ragmonk.documents.normalizer import NormalizedDocument


@dataclass(frozen=True)
class DocumentMetadata:
    title: str | None
    author: str | None
    page_count: int | None
    format: DocumentFormat
    is_scanned: bool
    source_filename: str


def extract_metadata(
    doc: DoclingDocument,
    normalized: NormalizedDocument,
    doc_format: DocumentFormat,
    path: Path,
) -> DocumentMetadata:
    return DocumentMetadata(
        title=normalized.title or path.stem,
        # Docling does not surface author/creator metadata for any backend
        # as of this version (its own project changelog lists metadata
        # extraction -- title/authors/references/language -- as "coming
        # soon"), so this is left None rather than guessed at.
        author=None,
        page_count=normalized.page_count,
        format=doc_format,
        is_scanned=normalized.is_scanned,
        source_filename=doc.origin.filename if doc.origin is not None else path.name,
    )
