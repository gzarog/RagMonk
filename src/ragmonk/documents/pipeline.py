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

from ragmonk.core.config import ChunkingConfig
from ragmonk.core.errors import RagMonkError
from ragmonk.core.models import Document, DocumentFormat, FileStatus, Paragraph, Section, Table
from ragmonk.documents import chunker, docling_adapter, normalizer
from ragmonk.documents.chunker import Chunk
from ragmonk.documents.docling_adapter import UnsupportedDocumentFormatError
from ragmonk.documents.metadata import extract_metadata
from ragmonk.indexing.coordinator import ProcessingOutcome, ProcessorContext
from ragmonk.indexing.incremental import VersionStamp
from ragmonk.retrieval import embedder
from ragmonk.sources.fingerprint import hash_file
from ragmonk.storage.repositories import documents_repo
from ragmonk.storage.sqlite import transaction


def _now() -> str:
    return datetime.now(UTC).isoformat()


def document_version_stamp() -> VersionStamp:
    """The document pipeline's current composite reuse identity (Search
    Quality Improvement Plan, Phase 12) -- ``IndexCoordinator`` compares
    this against a file's stored stamp (``indexing/incremental.
    decide_reprocessing``) to decide whether an unchanged-content
    document file still needs reprocessing.

    Reads each module's constant live (never cached) via plain attribute
    access, so a test's monkeypatch of e.g. ``chunker.CHUNKER_VERSION`` or
    ``embedder.EMBEDDING_MODEL_ID`` takes effect on this function's very
    next call, exactly as if that version had genuinely changed.
    """
    return VersionStamp(
        parser_version=docling_adapter.PARSER_VERSION,
        chunker_version=chunker.CHUNKER_VERSION,
        embedding_model_id=embedder.EMBEDDING_MODEL_ID,
        embedding_text_version=chunker.EMBEDDING_TEXT_VERSION,
    )


def document_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None:
        raise RagMonkError("DocumentProcessor requires a coordinator-provided ProcessorContext")

    try:
        doc_format = docling_adapter.detect_format(ctx.path)
    except UnsupportedDocumentFormatError:
        # A document extension this project intentionally does not
        # convert (legacy .doc/.ppt/.xls, .rtf, .rst, Outlook .msg -- see
        # docling_adapter.py's module docstring for why each one
        # specifically) -- index the file with no derived document
        # content rather than failing the run, mirroring
        # code/processor.py's "recognized extension, no grammar"
        # fallback.
        with transaction(ctx.conn):
            documents_repo.delete_by_file(ctx.conn, ctx.file_id)
        return ProcessingOutcome(status=FileStatus.INDEXED)

    if doc_format in docling_adapter.FORMATS_REQUIRING_IMAGE_OCR and not ctx.image_ocr:
        # Search Quality Improvement Plan, Phase 10: a raw image is
        # *detected* unconditionally (see sources/detector.py) but only
        # actually converted when a project has opted into
        # documents.image_ocr -- real extraction always means a full OCR
        # pass (see docling_adapter.py's docstring), so an image-heavy
        # source doesn't silently make every index run much slower
        # unless a project explicitly asks for that. Same "recognized,
        # no derived content" fallback as an unsupported extension.
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

    # ``ctx.ocr`` is ``None`` for any ``ProcessorContext`` built outside
    # the real coordinator (unit tests, mainly) -- falls back to "off",
    # Phase 3's original, only behavior, exactly like the coordinator's
    # own docstring for this field promises.
    conversion = docling_adapter.convert(ctx.path, conn=ctx.conn, ocr_mode=ctx.ocr or "off")
    normalized = normalizer.normalize(conversion.document, doc_format)
    # Metadata (title, in particular) is extracted before chunking rather
    # than after, unlike pre-Phase-3: `chunk_document` now bakes the
    # document title into every chunk's `search_text`/`contextual_text`
    # (see `documents/chunker.py`), so the title has to be known first.
    # `extract_metadata` only reads `conversion.document`/`normalized`, not
    # `chunks`, so reordering is safe.
    meta = extract_metadata(conversion.document, normalized, doc_format, ctx.path)
    chunks = chunker.chunk_document(
        normalized, config=ctx.chunking or ChunkingConfig(), doc_title=meta.title or ""
    )

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
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
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
                caption=chunk.caption,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
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
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
        )
