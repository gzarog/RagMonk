"""One source's full indexing pass: scan/diff/process
(``IndexCoordinator.run()``) plus Phase 4's cross-domain linking pass,
plus the offline/online status transition (Phase 7). This is the exact
unit of work ``ragmonk index`` runs per source; factored out here so the
Phase 7 daemon (``service/daemon.py``) triggers the same code path
instead of a parallel reimplementation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.code.processor import code_processor, code_version_stamp, prepare_code, publish_code
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
from ragmonk.indexing.embedding_indexer import prepare_embeddings, publish_embeddings
from ragmonk.knowledge.linker import link_touched_files
from ragmonk.retrieval import ann, embedder
from ragmonk.storage.repositories import embeddings_repo, sources_repo, vector_items_repo
from ragmonk.storage.sqlite import transaction
from ragmonk.telemetry.logging import get_logger, log_event

_logger = get_logger("indexing")


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
    registry.register(
        FileKind.CODE,
        code_processor,
        prepare=prepare_code,
        publish=publish_code,
        version_provider=code_version_stamp,
    )
    # Respects documents.enabled (core/config.py) -- when off, document-kind
    # files still index via the default raw processor (recorded, marked
    # INDEXED) just without Docling-derived content. Import deferred to here
    # (CLI performance improvement plan, Phase 2): documents.pipeline pulls
    # in Docling, which itself pulls in torch -- a cost a `version`/`status`/
    # `search` invocation that never touches this registry must not pay.
    if config.documents.enabled:
        from ragmonk.documents import docling_adapter
        from ragmonk.documents.pipeline import (
            document_processor,
            document_version_stamp,
            prepare_document,
            publish_document,
        )

        # Indexing optimization plan V2, Phase P2: registering prepare/
        # publish alongside the plain processor -- mirroring Phase P4's
        # CODE precedent immediately above -- is what makes DOCUMENT
        # eligible for the coordinator's bounded-parallel path. Opt-in via
        # config.indexing.document_extraction_workers, default 1
        # (serial), which never even looks at prepare/publish and keeps
        # calling document_processor exactly as before this phase.
        # prepare_setup caps torch's own thread pool once concurrent
        # extraction is actually engaged (see
        # docling_adapter.cap_native_thread_pools) -- a no-op whenever
        # document_extraction_workers is 1.
        registry.register(
            FileKind.DOCUMENT,
            document_processor,
            version_provider=document_version_stamp,
            prepare=prepare_document,
            publish=publish_document,
            prepare_setup=docling_adapter.cap_native_thread_pools,
        )
    # Indexing optimization plan V2, Phase P1: code_processor now also
    # registers a version_provider (code.processor.code_version_stamp).
    # Content_hash still catches every genuinely CHANGED file exactly as
    # before -- Tree-sitter reparses the whole file from source
    # (code/parser.py) any time content_hash changes -- but a parser/
    # extractor *code* change (with source bytes unchanged) previously had
    # no way to invalidate already-indexed entities/relationships/
    # code_fts at all. This closes that gap using the same generic
    # version-stamp comparison the document pipeline already exercises
    # (indexing/incremental.decide_reprocessing), scoped to CODE's own
    # parser_version axis. Its embeddings still get their own narrower
    # rebuild when only the model changes (see
    # indexing/embedding_indexer.py's CODE_EMBEDDING_TEXT_VERSION),
    # deliberately kept independent of this structural reprocess -- see
    # code_version_stamp's docstring.
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
    # Indexing optimization plan V2, Phase P6: exposes P3's persistent
    # embedding-cache reuse count on the returned struct too, not only
    # the DEBUG stage_timings log line -- lets a benchmark harness (or
    # any other in-process caller) read it without enabling debug
    # logging or parsing JSON log lines.
    embedding_cache_reused: int = 0


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
    # Storage backend abstraction plan, Phase 3: one backend per pass,
    # bound to this pass's own project connection -- handed to the
    # coordinator (so every queued file's ``publish`` half writes
    # through it) and reused below for the linking/embeddings stages,
    # so a whole source pass's persistence goes through the same
    # ``KnowledgeBackend`` instance/connection throughout.
    backend = LocalKnowledgeBackend(conn=conn)
    coordinator = IndexCoordinator(
        conn,
        source.id,
        source.path,
        source.include_patterns,
        source.exclude_patterns,
        ctx.config,
        processors=processors,
        backend=backend,
    )
    changed_paths = (
        scan_request.changed_paths if scan_request is not None and not scan_request.full else None
    )
    # Indexing optimization plan V2, Phase P5: the real trigger reason
    # for this pass -- "manual" for every ``scan_request``-less caller
    # (``ragmonk index``, matching ``ScanRequest.reason``'s own default),
    # otherwise whatever the daemon actually recorded (Phase P5 also
    # fixed ``service/daemon.py``'s own ``_build_scan_request``, which
    # previously discarded this into a constant ``"daemon"`` string).
    trigger_reason = scan_request.reason if scan_request is not None else "manual"
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
        log_event(
            _logger,
            "stage_timings",
            level=logging.DEBUG,
            source_id=source.id,
            trigger_reason=trigger_reason,
            source_offline=True,
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
        _linking_started = time.monotonic()
        with transaction(conn):
            linked = link_touched_files(
                conn,
                backend,
                source_id=source.id,
                touched_code_file_ids=result.touched_code_file_ids,
                touched_document_file_ids=result.touched_document_file_ids,
            )
        result.timings.linking_seconds = time.monotonic() - _linking_started

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
    cache_reused = 0
    if ctx.config.search.semantic and (embed_code_file_ids or embed_document_file_ids):
        touched_file_ids = [*embed_code_file_ids, *embed_document_file_ids]
        # Captured *before* the transaction below deletes-and-reinserts
        # vector_items for these files: the ANN index has no way to
        # discover on its own which ids just went stale, so this is the
        # only place that "before" snapshot is still available (blueprint
        # section 14).
        stale_vector_ids = vector_items_repo.list_vector_ids_by_file(conn, touched_file_ids)
        # Indexing optimization plan, Phase P5: model inference
        # (``prepare_embeddings``, potentially the slowest step in a
        # source pass) runs here, *before* the write transaction opens --
        # only the short delete+insert+stamp write (``publish_embeddings``)
        # below actually holds ``BEGIN IMMEDIATE``, unlike the pre-P5
        # shape where a single ``embed_touched_files`` call held that
        # write lock for as long as the model itself took to run.
        _embedding_started = time.monotonic()
        prepared = prepare_embeddings(
            conn,
            source_id=source.id,
            touched_code_file_ids=embed_code_file_ids,
            touched_document_file_ids=embed_document_file_ids,
            batch_size=ctx.config.indexing.embedding_batch_size,
        )
        # Indexing optimization plan V2, Phase P3: how many of this
        # batch's unique texts were served from the persistent
        # embedding-reuse cache without calling the model at all --
        # V2 Phase P5 telemetry surface for that phase's own feature.
        cache_reused = prepared.cache_reused if prepared is not None else 0
        with transaction(conn):
            embedded = (
                publish_embeddings(conn, prepared, backend=backend) if prepared is not None else 0
            )
        result.timings.embedding_seconds = time.monotonic() - _embedding_started
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
                _ann_started = time.monotonic()
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
                result.timings.ann_sync_seconds = time.monotonic() - _ann_started

    sources_repo.update_scan_result(
        ctx.sources_conn,
        source.id,
        last_scan_at=now,
        last_error=(f"{result.failed} file(s) failed" if result.failed else None),
        status=SourceStatus.ACTIVE,
        updated_at=now,
    )

    # Indexing optimization plan V2, Phase P5: one structured, DEBUG-level
    # event per pass carrying every stage duration plus the context
    # needed to explain them -- the real trigger reason (see
    # trigger_reason above), targeted/full mode and changed-path count
    # (Phase P2), worker counts (Phase P4/V2-P2), and embedding cache
    # reuse (V2 Phase P3). DEBUG, not INFO: ``configure_logging``'s
    # default level is "info" (``core/config.py``'s ``RuntimeConfig.
    # log_level``), so this never reaches the log file -- let alone the
    # console, which only ever surfaces WARNING+ regardless -- unless a
    # project explicitly sets ``runtime.log_level: debug``, matching this
    # codebase's one existing mechanism for "detailed but off by
    # default" telemetry rather than inventing a second config flag.
    # Building this dict of already-computed, cheap values (durations,
    # counts) costs nothing measurable even when the level check below
    # discards it -- see this phase's commit message for a timing check
    # confirming that.
    log_event(
        _logger,
        "stage_timings",
        level=logging.DEBUG,
        source_id=source.id,
        trigger_reason=trigger_reason,
        targeted=result.targeted,
        changed_path_count=(len(changed_paths) if changed_paths is not None else None),
        scan_seconds=result.timings.scan_seconds,
        classify_seconds=result.timings.classify_seconds,
        hash_seconds=result.timings.hash_seconds,
        hash_calls=result.timings.hash_calls,
        process_seconds=result.timings.process_seconds,
        linking_seconds=result.timings.linking_seconds,
        embedding_seconds=result.timings.embedding_seconds,
        ann_sync_seconds=result.timings.ann_sync_seconds,
        code_extraction_workers=ctx.config.indexing.code_extraction_workers,
        document_extraction_workers=ctx.config.indexing.document_extraction_workers,
        embedding_cache_reused=cache_reused,
        indexed=result.indexed,
        linked=linked,
        embedded=embedded,
    )

    return SourcePassResult(
        source=source,
        result=result,
        linked=linked,
        embedded=embedded,
        became_offline=False,
        became_online=became_online,
        embedding_cache_reused=cache_reused,
    )
