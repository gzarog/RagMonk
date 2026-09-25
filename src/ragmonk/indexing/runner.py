"""One source's full indexing pass: scan/diff/process
(``IndexCoordinator.run()``) plus Phase 4's cross-domain linking pass,
plus the offline/online status transition (Phase 7). This is the exact
unit of work ``ragmonk index`` runs per source; factored out here so the
Phase 7 daemon (``service/daemon.py``) triggers the same code path
instead of a parallel reimplementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.code.processor import code_processor, prepare_code, publish_code
from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import FileKind, Source, SourceStatus
from ragmonk.indexing.coordinator import (
    IndexCoordinator,
    IndexRunResult,
    ProcessorRegistry,
    ScanRequest,
    default_registry,
)
from ragmonk.indexing.embedding_indexer import embed_touched_files
from ragmonk.knowledge.linker import link_touched_files
from ragmonk.retrieval import ann, embedder
from ragmonk.storage.repositories import embeddings_repo, sources_repo, vector_items_repo
from ragmonk.storage.sqlite import transaction


def build_processor_registry(config: RagMonkConfig) -> ProcessorRegistry:
    """Shared by ``ragmonk index`` and the Phase 7 daemon -- both must
    dispatch a touched file to the exact same processors.
    """
    registry = default_registry()
    # Indexing optimization plan, Phase P4: registering prepare/publish
    # alongside the plain processor is what makes CODE eligible for the
    # coordinator's bounded-parallel path (see
    # ProcessorRegistry.supports_parallel_prepare) -- opt-in via
    # config.indexing.code_extraction_workers, default 1 (serial),
    # which never even looks at prepare/publish and keeps calling
    # code_processor exactly as before this phase.
    registry.register(FileKind.CODE, code_processor, prepare=prepare_code, publish=publish_code)
    # Respects documents.enabled (core/config.py) -- when off, document-kind
    # files still index via the default raw processor (recorded, marked
    # INDEXED) just without Docling-derived content. Import deferred to here
    # (CLI performance improvement plan, Phase 2): documents.pipeline pulls
    # in Docling, which itself pulls in torch -- a cost a `version`/`status`/
    # `search` invocation that never touches this registry must not pay.
    if config.documents.enabled:
        from ragmonk.documents.pipeline import document_processor, document_version_stamp

        registry.register(
            FileKind.DOCUMENT, document_processor, version_provider=document_version_stamp
        )
    # code_processor deliberately registers no version_provider (Search
    # Quality Improvement Plan, Phase 12): unlike the document pipeline, a
    # code file has no separate "chunker"/"parser" axis distinct from its
    # own content -- Tree-sitter reparses the whole file from source
    # (code/parser.py) on every CHANGED file already, so entities/
    # relationships/code_fts are always current the moment content_hash
    # changes, with nothing left for a version stamp to catch. Its
    # embeddings still get a narrower rebuild when only the model changes
    # (see indexing/embedding_indexer.py's CODE_EMBEDDING_TEXT_VERSION),
    # just not driven through IndexCoordinator's version-comparison path.
    return registry


@dataclass(frozen=True)
class SourcePassResult:
    source: Source
    result: IndexRunResult
    linked: int
    # Phase 9: vectors computed this pass -- always 0 when
    # ``search.semantic`` is off (the default), matching ``linked`` above
    # for a source with nothing touched. See ``indexing/embedding_indexer.py``.
    embedded: int
    # True only on the run that *changes* status -- lets a caller (CLI
    # print, daemon log line) announce a transition once rather than on
    # every steady-state ACTIVE/ACTIVE or OFFLINE/OFFLINE pass.
    became_offline: bool
    became_online: bool


def run_source_pass(
    ctx: AppContext,
    source: Source,
    processors: ProcessorRegistry,
    *,
    scan_request: ScanRequest | None = None,
) -> SourcePassResult:
    """``scan_request`` (indexing optimization plan, Phase P2), when
    given and not ``full``, drives a targeted pass over just its
    ``changed_paths`` instead of a full scan -- see
    ``IndexCoordinator.run``. ``None`` (``ragmonk index`` and every
    pre-P2 caller) keeps the original full-scan behavior unchanged.
    """
    project_id = paths.project_id_for_path(Path(source.path))
    conn = ctx.project_conn(project_id)
    coordinator = IndexCoordinator(
        conn,
        source.id,
        source.path,
        source.include_patterns,
        source.exclude_patterns,
        ctx.config,
        processors=processors,
    )
    changed_paths = (
        scan_request.changed_paths if scan_request is not None and not scan_request.full else None
    )
    result = coordinator.run(changed_paths=changed_paths)
    now = datetime.now(UTC).isoformat()

    if result.source_offline:
        became_offline = source.status is not SourceStatus.OFFLINE
        sources_repo.update_scan_result(
            ctx.sources_conn,
            source.id,
            last_scan_at=now,
            last_error=result.offline_reason,
            status=SourceStatus.OFFLINE,
            updated_at=now,
        )
        return SourcePassResult(
            source=source,
            result=result,
            linked=0,
            embedded=0,
            became_offline=became_offline,
            became_online=False,
        )

    became_online = source.status is SourceStatus.OFFLINE

    # Phase 4's cross-domain linking pass: deliberately run here, after
    # the per-file processor queue has fully drained, rather than inside
    # IndexCoordinator itself -- a link needs both a code entity and a
    # document to exist, so it cannot be computed per-file the way
    # Phase 2/3's atomic generational writes are, and IndexCoordinator
    # stays kind-agnostic (it does not import anything from code/ or
    # documents/ directly).
    linked = 0
    if result.touched_code_file_ids or result.touched_document_file_ids:
        with transaction(conn):
            linked = link_touched_files(
                conn,
                touched_code_file_ids=result.touched_code_file_ids,
                touched_document_file_ids=result.touched_document_file_ids,
            )

    # Phase 9: same touched-files scoping and same "run after the queue
    # has drained" placement as the linking pass above, gated behind
    # ``search.semantic`` so a project that never turns it on pays
    # nothing extra here. ``retrieval/embedder.py`` only imports
    # ``torch``/``transformers`` lazily, inside the function this branch
    # is the sole caller of, so leaving ``search.semantic`` off also means
    # those heavy libraries are never actually loaded into the process.
    # Search Quality Improvement Plan, Phase 12: embeddings_stale_*_file_ids
    # (content unchanged, but the stored embedding version stamp is --
    # see IndexCoordinator._version_reprocess_decision) are unioned in
    # here, not into `result.touched_*_file_ids` themselves -- the linking
    # pass just above stays scoped to genuinely touched files only, since
    # relinking a file whose entities/document sections never changed
    # would be pure waste.
    embed_code_file_ids = [*result.touched_code_file_ids, *result.embeddings_stale_code_file_ids]
    embed_document_file_ids = [
        *result.touched_document_file_ids,
        *result.embeddings_stale_document_file_ids,
    ]
    embedded = 0
    if ctx.config.search.semantic and (embed_code_file_ids or embed_document_file_ids):
        touched_file_ids = [*embed_code_file_ids, *embed_document_file_ids]
        # Captured *before* the transaction below deletes-and-reinserts
        # vector_items for these files: the ANN index has no way to
        # discover on its own which ids just went stale, so this is the
        # only place that "before" snapshot is still available (blueprint
        # section 14).
        stale_vector_ids = vector_items_repo.list_vector_ids_by_file(conn, touched_file_ids)
        with transaction(conn):
            embedded = embed_touched_files(
                conn,
                source_id=source.id,
                touched_code_file_ids=embed_code_file_ids,
                touched_document_file_ids=embed_document_file_ids,
            )
        if embedded:
            # Deliberately outside the transaction above: the ANN index
            # is a separate on-disk file, not part of the SQLite
            # transaction's atomicity guarantee -- SQLite (already
            # committed at this point) remains the authoritative source
            # it can always be rebuilt from (blueprint section 49), so a
            # failure here degrades to "ANN index lags until the next
            # sync or an explicit `ragmonk vectors rebuild`", never to
            # a corrupt or half-written knowledge.db.
            dim = embeddings_repo.get_dim_for_model(conn, embedder.EMBEDDING_MODEL_ID)
            if dim is not None:
                ann.sync_index_for_files(
                    conn,
                    project_id=project_id,
                    home=ctx.home,
                    engine=ctx.config.search.vector.engine,
                    ndim=dim,
                    model_id=embedder.EMBEDDING_MODEL_ID,
                    removed_vector_ids=stale_vector_ids,
                    touched_file_ids=touched_file_ids,
                    rebuild_deleted_ratio=ctx.config.search.vector.rebuild_deleted_ratio,
                )

    sources_repo.update_scan_result(
        ctx.sources_conn,
        source.id,
        last_scan_at=now,
        last_error=(f"{result.failed} file(s) failed" if result.failed else None),
        status=SourceStatus.ACTIVE,
        updated_at=now,
    )
    return SourcePassResult(
        source=source,
        result=result,
        linked=linked,
        embedded=embedded,
        became_offline=False,
        became_online=became_online,
    )
