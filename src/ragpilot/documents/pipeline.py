"""``document_processor``: the Phase 3 processor registered against
``FileKind.DOCUMENT`` in ``indexing.coordinator.ProcessorRegistry``.

Orchestrates format detection -> Docling conversion -> normalization ->
chunking -> storage, and performs the atomic "delete previous generation,
insert new document/sections/FTS rows" step described in
``storage/schema.py``. Anything ``docling_adapter.convert`` raises (a
genuine conversion failure) is left to propagate: ``IndexCoordinator.
_process_queue`` already catches, records and retries/fails processor
exceptions per file without aborting the run, exactly as it does for
``code.processor.code_processor``.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from ragpilot.core.config import ChunkingConfig
from ragpilot.core.errors import RagpilotError
from ragpilot.core.models import Document, DocumentFormat, FileStatus, Paragraph, Section, Table
from ragpilot.documents import chunker, docling_adapter, normalizer
from ragpilot.documents.chunker import Chunk
from ragpilot.documents.docling_adapter import UnsupportedDocumentFormatError
from ragpilot.documents.metadata import extract_metadata
from ragpilot.indexing.coordinator import ProcessingOutcome, ProcessorContext
from ragpilot.sources.fingerprint import hash_file
from ragpilot.storage.repositories import documents_repo
from ragpilot.storage.sqlite import transaction


def _now() -> str:
    return datetime.now(UTC).isoformat()


def document_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None:
        raise RagpilotError("DocumentProcessor requires a coordinator-provided ProcessorContext")

    try:
        doc_format = docling_adapter.detect_format(ctx.path)
    except UnsupportedDocumentFormatError:
        # A document extension Phase 3 intentionally does not convert yet
        # (legacy .doc/.ppt/.xls, OpenDocument, .rtf, .csv, .rst -- see
        # docling_adapter.EXTENSION_TO_FORMAT) -- index the file with no
        # derived document content rather than failing the run, mirroring
        # code/processor.py's "recognized extension, no grammar" fallback.
        with transaction(ctx.conn):
            documents_repo.delete_by_file(ctx.conn, ctx.file_id)
        return ProcessingOutcome(status=FileStatus.INDEXED)

    if doc_format is DocumentFormat.PDF and ctx.max_document_pages is not None:
        # Checked via pypdfium2 alone (see docling_adapter.pdf_page_count),
        # before Docling's own conversion -- so an oversized PDF never
        # triggers the layout/table-structure model download at all.
        page_count = docling_adapter.pdf_page_count(ctx.path)
        if page_count > ctx.max_document_pages:
            return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)

    conversion = docling_adapter.convert(ctx.path, conn=ctx.conn)
    normalized = normalizer.normalize(conversion.document, doc_format)
    chunks = chunker.chunk_document(normalized, config=ctx.chunking or ChunkingConfig())
    meta = extract_metadata(conversion.document, normalized, doc_format, ctx.path)

    now = _now()
    document_id = uuid.uuid4().hex
    chunk_ids = [uuid.uuid4().hex for _ in chunks]

    document = Document(
        id=document_id,
        source_id=ctx.source_id,
        file_id=ctx.file_id,
        format=doc_format,
        title=meta.title,
        author=meta.author,
        page_count=meta.page_count,
        section_count=sum(1 for c in chunks if c.kind == "heading"),
        paragraph_count=sum(1 for c in chunks if c.kind == "paragraph"),
        table_count=sum(1 for c in chunks if c.kind == "table"),
        is_scanned=meta.is_scanned,
        content_hash=hash_file(ctx.path),
        generation=ctx.next_generation,
        created_at=now,
        updated_at=now,
    )

    doc_title = meta.title or ""

    with transaction(ctx.conn):
        documents_repo.delete_by_file(ctx.conn, ctx.file_id)
        documents_repo.insert_document(ctx.conn, document)
        for index, (chunk_id, chunk) in enumerate(zip(chunk_ids, chunks, strict=True)):
            parent_id = chunk_ids[chunk.parent_index] if chunk.parent_index is not None else None
            _insert_chunk(
                ctx.conn,
                chunk_id=chunk_id,
                document_id=document_id,
                file_id=ctx.file_id,
                parent_id=parent_id,
                order_index=index,
                chunk=chunk,
                generation=ctx.next_generation,
                created_at=now,
                doc_title=doc_title,
            )

    return ProcessingOutcome(status=FileStatus.INDEXED)


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    file_id: str,
    parent_id: str | None,
    order_index: int,
    chunk: Chunk,
    generation: int,
    created_at: str,
    doc_title: str,
) -> None:
    heading_path = list(chunk.heading_path)
    if chunk.kind == "heading":
        documents_repo.insert_section(
            conn,
            Section(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                heading_level=chunk.heading_level or 0,
                text=chunk.text,
                heading_path=heading_path,
                parent_id=parent_id,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
        )
    elif chunk.kind == "table":
        rows = [list(row) for row in (chunk.table_rows or ())]
        documents_repo.insert_table(
            conn,
            Table(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                heading_path=heading_path,
                parent_id=parent_id,
                rows=rows,
                num_rows=len(rows),
                num_cols=len(rows[0]) if rows else 0,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
        )
    else:
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                text=chunk.text,
                heading_path=heading_path,
                parent_id=parent_id,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
        )
