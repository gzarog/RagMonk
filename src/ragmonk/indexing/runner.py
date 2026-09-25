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
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.backends.models import FileRecord as BackendFileRecord
from ragmonk.code.processor import code_processor, code_version_stamp, prepare_code, publish_code
from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import FileKind, FileRecord, FileStatus, Source, SourceStatus
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
from ragmonk.storage.repositories import (
    embeddings_repo,
    files_repo,
    sources_repo,
    vector_items_repo,
)
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


def generation_as_int(generation: str) -> int:
    """The int form of a backend generation id, for
    ``ProcessorContext.next_generation``/``PreparedCode.generation``.
    Generation ids are decimal strings in practice; a non-decimal id
    (defensive) maps to a stable crc32 so the same string always yields
    the same int.
    """
    try:
        return int(generation)
    except ValueError:
        return zlib.crc32(generation.encode("utf-8"))


def select_pass_backend(ctx: AppContext, conn: object) -> KnowledgeBackend:
    """Completion plan F1/F2: the ONE place indexing picks its
    searchable-knowledge writer when none is injected explicitly.

    - ``storage.mode == "local"``: a ``LocalKnowledgeBackend`` bound to
      the pass's own per-project SQLite connection (unchanged behavior).
    - ``storage.mode == "server"``: ``ctx.backend()`` -- the configured
      OpenSearch/Elasticsearch adapter. Local SQLite is then only the
      control plane (scan/diff state, file status, jobs, retries,
      version/embedding bookkeeping), never a searchable-knowledge store.
    """
    if ctx.config.storage.mode == "server":
        return ctx.backend()
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    return LocalKnowledgeBackend(conn=conn)


def run_source_pass(
    ctx: AppContext,
    source: Source,
    processors: ProcessorRegistry,
    *,
    scan_request: ScanRequest | None = None,
    backend: KnowledgeBackend | None = None,
    force_generation: int | None = None,
) -> SourcePassResult:
    """The single shared indexing entrypoint for ``ragmonk index``, the
    daemon, the Admin UI background indexer and ``ragmonk rebuild``.

    Completion plan F1/F2: backend selection is centralized here. With no
    ``backend`` injected, ``select_pass_backend`` picks the local SQLite
    backend in local mode and the configured server backend in server
    mode -- every caller gets the right writer automatically. An explicit
    ``backend``/``force_generation`` (``ops/rebuild.py``'s generation-
    wrapped server rebuild, tests) is honored as-is.

    Server mode, no explicit generation: the pass writes into the
    source's *published* generation (an incremental update, immediately
    visible), except when nothing has ever been published for the source
    -- then the whole first pass runs inside a fresh generation
    (``begin_generation`` -> pass -> ``publish_generation``, or
    ``abort_generation`` on any raised failure) so a half-finished first
    index is never observable.
    """
    if backend is not None or ctx.config.storage.mode != "server":
        return _run_source_pass(
            ctx,
            source,
            processors,
            scan_request=scan_request,
            backend=backend,
            force_generation=force_generation,
        )

    server_backend = ctx.backend()
    server_backend.ensure_schema()
    published = server_backend.published_generation(source.id)
    if published is not None:
        return _run_source_pass(
            ctx,
            source,
            processors,
            scan_request=scan_request,
            backend=server_backend,
            force_generation=generation_as_int(published),
        )

    # First publication for this source: the local control-plane scan
    # state may still claim files are indexed (e.g. a switch from local
    # mode, or an earlier aborted first pass) -- reset it so every file is
    # (re)published into the new generation, exactly like a rebuild.
    _reset_control_plane(ctx, source)
    generation = server_backend.begin_generation(source.id)
    try:
        pass_result = _run_source_pass(
            ctx,
            source,
            processors,
            scan_request=None,
            backend=server_backend,
            force_generation=generation_as_int(generation),
        )
    except BaseException:
        server_backend.abort_generation(source.id, generation)
        raise
    if pass_result.result.source_offline:
        server_backend.abort_generation(source.id, generation)
    else:
        server_backend.publish_generation(source.id, generation)
    return pass_result


def _reset_control_plane(ctx: AppContext, source: Source) -> None:
    project_id = paths.project_id_for_path(Path(source.path))
    ctx.close_project_conn(project_id)
    db_path = paths.project_db_path(project_id, ctx.home)
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _sync_server_files(
    conn: object,
    backend: KnowledgeBackend,
    source_id: str,
    generation: int,
    *,
    allow_deletes: bool,
) -> None:
    """Completion plan F1/F6: mirror the local control-plane file table
    for ``source_id`` into the server backend's (generation-tagged) file
    records -- upserting new/changed/renamed files and deleting every
    server artifact of a file the control plane no longer has (the
    server-side equivalent of local mode's ``files`` FK cascade).
    ``allow_deletes`` is False for a pass whose scan was incomplete or
    whose source went offline (never infer "missing means deleted").
    """
    import sqlite3

    assert isinstance(conn, sqlite3.Connection)
    local_files = files_repo.list_by_source(conn, source_id)
    local_by_id: dict[str, FileRecord] = {f.id: f for f in local_files}
    published = backend.published_generation(source_id)
    if published is None or generation_as_int(published) != generation:
        # Writing a *new* (unpublished) generation -- a rebuild or a
        # first publication: it starts empty, so every local file is
        # upserted into it and nothing is deleted. The published
        # generation's records (possibly under different file ids, since
        # a rebuild resets the control plane) must stay untouched until
        # ``publish_generation`` swaps and garbage-collects them.
        server_by_id: dict[str, BackendFileRecord] = {}
        allow_deletes = False
    else:
        server_by_id = {r.file_id: r for r in backend.list_files(source_id)}
    gen_int = generation
    upserts: list[BackendFileRecord] = []
    for record in local_files:
        if record.status not in (FileStatus.INDEXED, FileStatus.SKIPPED_LIMIT):
            continue
        existing = server_by_id.get(record.id)
        if (
            existing is not None
            and existing.path == record.path
            and existing.content_hash == (record.content_hash or "")
            and existing.generation == gen_int
            and (existing.metadata or {}).get("status") == record.status.value
        ):
            continue
        upserts.append(
            BackendFileRecord(
                file_id=record.id,
                source_id=source_id,
                path=record.path,
                content_hash=record.content_hash or "",
                size_bytes=record.size,
                mtime=record.mtime,
                metadata={
                    "kind": record.kind.value,
                    "status": record.status.value,
                    "last_indexed_at": record.last_indexed_at,
                    "updated_at": record.updated_at,
                },
                generation=gen_int,
            )
        )
    backend.upsert_files(upserts)
    if allow_deletes:
        for file_id in server_by_id:
            if file_id not in local_by_id:
                backend.delete_file(source_id, file_id)


def _run_source_pass(
    ctx: AppContext,
    source: Source,
    processors: ProcessorRegistry,
    *,
    scan_request: ScanRequest | None = None,
    backend: KnowledgeBackend | None = None,
    force_generation: int | None = None,
) -> SourcePassResult:
    """``scan_request`` (indexing optimization plan, Phase P2), when
    given and not ``full``, drives a targeted pass over just its
    ``changed_paths`` instead of a full scan -- see
    ``IndexCoordinator.run``. ``None`` (``ragmonk index`` and every
    pre-P2 caller) keeps the original full-scan behavior unchanged.

    ``backend``/``force_generation`` (Storage backend abstraction plan,
    Phase 7): a server-mode full rebuild (``ops/rebuild.py``) passes its
    own ``KnowledgeBackend`` (the cached server adapter from
    ``ctx.backend()``) plus the int form of the generation id
    ``begin_generation`` returned, so this pass's writes land in the
    server backend, tagged with that exact generation, instead of the
    default local SQLite path below. ``None`` for both (every other
    caller -- ``ragmonk index``, the daemon, a plain non-server
    rebuild) keeps this pass's pre-Phase-7 behavior: a fresh
    ``LocalKnowledgeBackend`` bound to this pass's own project
    connection, and the usual per-file ``file.generation + 1`` bump.
    """
    project_id = paths.project_id_for_path(Path(source.path))
    # control_plane=True: even in server mode this pass still needs its
    # own local ``conn`` -- the coordinator's scan/diff/generation
    # bookkeeping and file-status tracking (Phase 3/7) live in local
    # sqlite regardless of ``storage.mode``; only the *published*
    # knowledge (entities/documents/embeddings) is redirected to the
    # server backend, via the ``backend`` argument below, when one is
    # supplied by a server-mode caller (``ops/rebuild.py``).
    conn = ctx.project_conn(project_id, control_plane=True)
    # Storage backend abstraction plan, Phase 3: one backend per pass,
    # bound to this pass's own project connection -- handed to the
    # coordinator (so every queued file's ``publish`` half writes
    # through it) and reused below for the linking/embeddings stages,
    # so a whole source pass's persistence goes through the same
    # ``KnowledgeBackend`` instance/connection throughout.
    if backend is None:
        backend = select_pass_backend(ctx, conn)
    server = backend.is_server
    if server and force_generation is None:
        # Explicit server backend but no generation (a direct caller):
        # write into the source's published generation.
        published = backend.published_generation(source.id)
        force_generation = generation_as_int(published) if published is not None else 0
    coordinator = IndexCoordinator(
        conn,
        source.id,
        source.path,
        source.include_patterns,
        source.exclude_patterns,
        ctx.config,
        processors=processors,
        backend=backend,
        force_generation=force_generation,
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

    if server:
        assert force_generation is not None
        _sync_server_files(
            conn,
            backend,
            source.id,
            force_generation,
            allow_deletes=not result.source_offline and not result.scan_incomplete,
        )

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
                generation=force_generation if server else None,
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
        stale_vector_ids = (
            [] if server else vector_items_repo.list_vector_ids_by_file(conn, touched_file_ids)
        )
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
            backend=backend,
            generation=str(force_generation) if server else None,
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
        if embedded and not server:
            # Server mode: the vectors live in the server engine's own
            # kNN index -- there is no local ANN index to sync.
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
