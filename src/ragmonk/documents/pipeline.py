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

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ragmonk.backends.models import PreparedDocument as BackendPreparedDocument
from ragmonk.core.config import ChunkingConfig
from ragmonk.core.errors import ContentChangedDuringProcessingError, RagMonkError
from ragmonk.core.models import Document, DocumentFormat, FileStatus
from ragmonk.documents import chunker, docling_adapter, normalizer
from ragmonk.documents.chunker import Chunk, ChunkingDiagnostics
from ragmonk.documents.docling_adapter import UnsupportedDocumentFormatError
from ragmonk.documents.metadata import DocumentMetadata, extract_metadata
from ragmonk.indexing.coordinator import ProcessingOutcome, ProcessorContext
from ragmonk.indexing.incremental import VersionStamp
from ragmonk.retrieval import embedder
from ragmonk.sources.fingerprint import stat_unchanged, verified_hash
from ragmonk.storage.sqlite import transaction
from ragmonk.telemetry.logging import get_logger, log_event

_logger = get_logger("documents")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _log_chunking_diagnostics(
    ctx: ProcessorContext,
    config: ChunkingConfig,
    diagnostics: chunker.ChunkingDiagnostics,
) -> None:
    """Exact Tokenizer plan, Phase 5: emit a structured event summarizing
    how the exact token budget shaped this document's chunks -- context
    reductions, budget splits, oversized-table segmentation and the
    largest observed embedding payload -- and, critically, a WARNING if
    any payload broke the no-silent-truncation invariant (which must never
    happen).
    """
    bound = config.resolved_max_tokens - config.safety_tokens
    violated = diagnostics.max_payload_tokens > bound
    noteworthy = (
        violated
        or diagnostics.chunks_split_by_budget
        or diagnostics.context_headers_reduced
        or diagnostics.oversized_table_rows
        or diagnostics.oversized_table_cells
    )
    if not noteworthy:
        return
    log_event(
        _logger,
        "chunk_budget_invariant_violation" if violated else "chunk_budget_diagnostics",
        level=logging.WARNING if violated else logging.INFO,
        path=str(ctx.path),
        max_payload_tokens=diagnostics.max_payload_tokens,
        payload_budget=bound,
        chunks_split_by_budget=diagnostics.chunks_split_by_budget,
        context_headers_reduced=diagnostics.context_headers_reduced,
        oversized_table_rows=diagnostics.oversized_table_rows,
        oversized_table_cells=diagnostics.oversized_table_cells,
    )


def _tokenizer_index_identity() -> str:
    """The exact tokenizer's contribution to the chunk-derivation identity
    (Exact Tokenizer plan, Phase 4).

    Chunk boundaries depend on the exact tokenizer's *bytes* and on the
    model's maximum sequence length (the budget ceiling), so the pinned
    revision, the asset fingerprint and the max-sequence-length are all
    folded into ``chunker_version``. Bumping the pinned tokenizer -- even
    with no chunker code change -- therefore changes the stored stamp and
    triggers the existing graceful reprocessing
    (``indexing/incremental.decide_reprocessing``) for every affected file
    on the next index run; no hard index rejection is needed. Read live
    (never cached) so a test's monkeypatch of the identity takes effect
    immediately.
    """
    from ragmonk.tokenization import model_identity

    return (
        f"tok:{model_identity.TOKENIZER_REVISION}"
        f":{model_identity.tokenizer_fingerprint()}"
        f":max{model_identity.MAX_SEQUENCE_TOKENS}"
    )


def document_version_stamp() -> VersionStamp:
    """The document pipeline's current composite reuse identity
    (``IndexCoordinator`` compares this against a file's stored stamp --
    ``indexing/incremental.decide_reprocessing`` -- to decide whether an
    unchanged-content document file still needs reprocessing).

    Reads each module's constant live (never cached) via plain attribute
    access, so a test's monkeypatch of e.g. ``chunker.CHUNKER_VERSION`` or
    ``embedder.EMBEDDING_MODEL_ID`` takes effect on this function's very
    next call, exactly as if that version had genuinely changed.

    Exact Tokenizer plan, Phase 4: ``chunker_version`` now embeds the
    exact tokenizer's identity (see ``_tokenizer_index_identity``) so the
    index's derivation is provably tied to the tokenizer that produced it,
    and a tokenizer change reprocesses affected files gracefully.
    """
    return VersionStamp(
        parser_version=docling_adapter.PARSER_VERSION,
        chunker_version=f"{chunker.CHUNKER_VERSION}+{_tokenizer_index_identity()}",
        embedding_model_id=embedder.EMBEDDING_MODEL_ID,
        embedding_text_version=chunker.EMBEDDING_TEXT_VERSION,
    )


@dataclass(frozen=True)
class PreparedDocument:
    """``prepare_document``'s result: everything Docling conversion,
    normalization and chunking produced for one file -- no database
    access at all beyond the read-only document-conversion cache lookup
    (``cache_conn``, a caller-supplied connection kept deliberately
    separate from ``ProcessorContext.conn`` -- see ``prepare_document``'s
    docstring for why). Safe to build concurrently across files, exactly
    like ``code.processor.PreparedCode``.

    ``status`` set (``SKIPPED_LIMIT``) means "stop here, nothing else is
    populated, ``publish_document`` should just return this status".
    ``delete_only`` means "this file is recognized but has no document
    content to derive (unsupported extension, or an image with
    ``image_ocr`` off) -- ``publish_document`` should delete any prior
    generation's rows and mark it indexed, nothing more". Otherwise every
    other field is populated and ``publish_document`` writes a full new
    generation from them.
    """

    status: FileStatus | None = None
    delete_only: bool = False
    doc_format: DocumentFormat | None = None
    content_hash: str | None = None
    meta: DocumentMetadata | None = None
    chunks: list[Chunk] | None = None


def prepare_document(
    ctx: ProcessorContext, *, cache_conn: sqlite3.Connection | None
) -> PreparedDocument:
    """Docling conversion, normalization and chunking -- the potentially
    slow, CPU/IO-heavy half of document processing -- with no write
    transaction held on ``ctx.conn`` at any point, so it's safe to run
    this concurrently across files (indexing optimization plan V2, Phase
    P2's bounded ``indexing.document_extraction_workers`` pool).

    ``cache_conn`` is deliberately a *separate* connection from
    ``ctx.conn``, never that same shared connection object: the document-
    conversion cache lookup/store inside ``docling_adapter.convert`` does
    read and write SQLite (``document_conversion_cache``), and a
    ``sqlite3.Connection`` is not safe to use concurrently from more than
    one thread. The coordinator's parallel path hands this function one
    dedicated connection per worker thread (see
    ``indexing/coordinator.py``'s ``_PerThreadConnections``); the serial
    (default, ``document_extraction_workers=1``) path -- via
    ``document_processor`` below -- simply passes ``ctx.conn`` itself,
    exactly matching this function's pre-P2 behavior since there is only
    ever one caller of it at a time in that mode. ``None`` (a
    coordinator-external ``ProcessorContext``, e.g. most unit tests) means
    "convert without a cache", same as every pre-P2 caller.
    """
    if ctx.size > ctx.max_size_bytes:
        return PreparedDocument(status=FileStatus.SKIPPED_LIMIT)

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
        return PreparedDocument(delete_only=True)

    if doc_format in docling_adapter.FORMATS_REQUIRING_IMAGE_OCR and not ctx.image_ocr:
        # Search Quality Improvement Plan, Phase 10: a raw image is
        # *detected* unconditionally (see sources/detector.py) but only
        # actually converted when a project has opted into
        # documents.image_ocr -- real extraction always means a full OCR
        # pass (see docling_adapter.py's docstring), so an image-heavy
        # source doesn't silently make every index run much slower
        # unless a project explicitly asks for that. Same "recognized,
        # no derived content" fallback as an unsupported extension.
        return PreparedDocument(delete_only=True)

    if doc_format is DocumentFormat.PDF and ctx.max_document_pages is not None:
        # Checked via pypdfium2 alone (see docling_adapter.pdf_page_count),
        # before Docling's own conversion -- so an oversized PDF never
        # triggers the layout/table-structure model download at all.
        page_count = docling_adapter.pdf_page_count(ctx.path)
        if page_count > ctx.max_document_pages:
            return PreparedDocument(status=FileStatus.SKIPPED_LIMIT)

    # Indexing optimization plan, Phase P3 / finding F4: reuses the
    # coordinator's already-computed digest (via ``ctx.file_identity``)
    # instead of this processor independently re-reading and re-hashing
    # the whole file, as long as a cheap stat still matches what the
    # coordinator saw. Threaded into ``convert`` below too, so the PDF
    # conversion cache's own lookup (``docling_adapter._convert_pdf``)
    # doesn't hash the file a third time.
    content_hash = verified_hash(ctx.path, expected=ctx.file_identity)

    # ``ctx.ocr`` is ``None`` for any ``ProcessorContext`` built outside
    # the real coordinator (unit tests, mainly) -- falls back to "off",
    # Phase 3's original, only behavior, exactly like the coordinator's
    # own docstring for this field promises.
    conversion = docling_adapter.convert(
        ctx.path, conn=cache_conn, ocr_mode=ctx.ocr or "off", content_hash=content_hash
    )
    normalized = normalizer.normalize(conversion.document, doc_format)
    # Metadata (title, in particular) is extracted before chunking rather
    # than after, unlike pre-Phase-3: `chunk_document` now bakes the
    # document title into every chunk's `search_text`/`contextual_text`
    # (see `documents/chunker.py`), so the title has to be known first.
    # `extract_metadata` only reads `conversion.document`/`normalized`, not
    # `chunks`, so reordering is safe.
    meta = extract_metadata(conversion.document, normalized, doc_format, ctx.path)
    chunking_config = ctx.chunking or ChunkingConfig()
    chunk_diagnostics = ChunkingDiagnostics()
    chunks = chunker.chunk_document(
        normalized,
        config=chunking_config,
        doc_title=meta.title or "",
        diagnostics=chunk_diagnostics,
    )
    _log_chunking_diagnostics(ctx, chunking_config, chunk_diagnostics)

    return PreparedDocument(
        doc_format=doc_format, content_hash=content_hash, meta=meta, chunks=chunks
    )


def publish_document(ctx: ProcessorContext, prepared: PreparedDocument) -> ProcessingOutcome:
    """Builds ``prepared``'s ``Document``/chunks and hands them to
    ``KnowledgeBackend.publish_document`` for the atomic delete-old-
    generation/insert-new-generation write -- always on whichever thread
    calls this, which the coordinator guarantees is always its single
    writer thread, never a parallel prepare worker (Phase P2's "one
    transactional publisher per project" rule, mirroring
    ``code.processor.publish_code``).

    Storage backend abstraction plan, Phase 3: only the write moved
    behind the backend contract -- see ``LocalKnowledgeBackend.
    publish_document``. ``ctx.backend`` is used when the coordinator
    supplied one; a coordinator-external caller (most unit tests) gets a
    ``LocalKnowledgeBackend`` constructed on demand, bound to the same
    ``ctx.conn``.
    """
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None:
        raise RagMonkError("DocumentProcessor requires a coordinator-provided ProcessorContext")

    backend = ctx.backend
    if backend is None:
        from ragmonk.backends.local import LocalKnowledgeBackend

        backend = LocalKnowledgeBackend(conn=ctx.conn)

    if prepared.status is not None:
        return ProcessingOutcome(status=prepared.status)

    if prepared.delete_only:
        # No content was derived (unsupported extension, or an
        # image with image_ocr off) -- nothing to identity-recheck
        # either, matching document_processor's pre-P2 behavior, which
        # never reached the check for these two cases.
        with transaction(ctx.conn):
            backend.publish_document(
                BackendPreparedDocument(
                    file_id=ctx.file_id, source_id=ctx.source_id, delete_only=True
                )
            )
        return ProcessingOutcome(status=FileStatus.INDEXED)

    assert (
        prepared.doc_format is not None
        and prepared.content_hash is not None
        and prepared.meta is not None
        and prepared.chunks is not None
    )
    doc_format = prepared.doc_format
    content_hash = prepared.content_hash
    meta = prepared.meta
    chunks = prepared.chunks

    # Indexing optimization plan, Phase P3 (serial path)/P2 (bounded
    # parallel path): a final, cheap check that the file is still the
    # one the coordinator scanned and ``prepare_document`` just spent
    # potentially real time converting/chunking -- a narrow but real
    # race (a concurrent write landing mid-extraction, widened further
    # by however long this file sat in the bounded in-flight queue
    # before this publish call ran) must never end in silently
    # publishing content derived from a file that no longer looks like
    # that on disk. Only meaningful when the coordinator supplied an
    # identity in the first place; a coordinator-external
    # ``ProcessorContext`` has nothing to compare against and keeps its
    # pre-P3 behavior.
    if ctx.file_identity is not None:
        try:
            post_stat = ctx.path.stat()
        except OSError as exc:
            raise ContentChangedDuringProcessingError(
                f"{ctx.path}: file became unreadable during processing: {exc}"
            ) from exc
        if not stat_unchanged(
            ctx.file_identity.size, ctx.file_identity.mtime, post_stat.st_size, post_stat.st_mtime
        ):
            raise ContentChangedDuringProcessingError(
                f"{ctx.path}: file changed during processing; will be retried"
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
        content_hash=content_hash,
        generation=ctx.next_generation,
        created_at=now,
        updated_at=now,
    )

    doc_title = meta.title or ""

    with transaction(ctx.conn):
        backend.publish_document(
            BackendPreparedDocument(
                file_id=ctx.file_id,
                source_id=ctx.source_id,
                generation=ctx.next_generation,
                document=document,
                chunk_ids=chunk_ids,
                chunks=chunks,
                doc_title=doc_title,
            )
        )

    return ProcessingOutcome(status=FileStatus.INDEXED)


def document_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    """The registered ``FileKind.DOCUMENT`` processor -- ``prepare_document``
    then ``publish_document`` run back-to-back on whichever thread calls
    this, exactly the serial, single-call behavior this function always
    had. The coordinator's optional bounded-parallel path
    (``indexing.document_extraction_workers`` > 1) calls
    ``prepare_document``/``publish_document`` directly instead (see
    ``ProcessorRegistry``'s ``prepare``/``publish`` registration), never
    through this function -- this wrapper exists only for the (default,
    ``document_extraction_workers=1``) serial path and any direct caller
    (tests, mainly) that wants the simple one-call interface. Passes
    ``ctx.conn`` itself as ``prepare_document``'s cache connection: safe
    here specifically because this wrapper is only ever called once at a
    time (no concurrent prepare calls sharing it), unlike the parallel
    path's dedicated per-worker connections.
    """
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None:
        raise RagMonkError("DocumentProcessor requires a coordinator-provided ProcessorContext")
    prepared = prepare_document(ctx, cache_conn=ctx.conn)
    return publish_document(ctx, prepared)

