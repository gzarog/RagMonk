"""One source's full indexing pass: scan/diff/process
(``IndexCoordinator.run()``) plus Phase 4's cross-domain linking pass,
plus the offline/online status transition (Phase 7). This is the exact
unit of work ``ragpilot index`` runs per source; factored out here so the
Phase 7 daemon (``service/daemon.py``) triggers the same code path
instead of a parallel reimplementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragpilot.code.processor import code_processor
from ragpilot.core import paths
from ragpilot.core.config import RagpilotConfig
from ragpilot.core.lifecycle import AppContext
from ragpilot.core.models import FileKind, Source, SourceStatus
from ragpilot.indexing.coordinator import (
    IndexCoordinator,
    IndexRunResult,
    ProcessorRegistry,
    default_registry,
)
from ragpilot.indexing.embedding_indexer import embed_touched_files
from ragpilot.knowledge.linker import link_touched_files
from ragpilot.retrieval import ann, embedder
from ragpilot.storage.repositories import embeddings_repo, sources_repo, vector_items_repo
from ragpilot.storage.sqlite import transaction


def build_processor_registry(config: RagpilotConfig) -> ProcessorRegistry:
    """Shared by ``ragpilot index`` and the Phase 7 daemon -- both must
    dispatch a touched file to the exact same processors.
    """
    registry = default_registry()
    registry.register(FileKind.CODE, code_processor)
    # Respects documents.enabled (core/config.py) -- when off, document-kind
    # files still index via the default raw processor (recorded, marked
    # INDEXED) just without Docling-derived content. Import deferred to here
    # (CLI performance improvement plan, Phase 2): documents.pipeline pulls
    # in Docling, which itself pulls in torch -- a cost a `version`/`status`/
    # `search` invocation that never touches this registry must not pay.
    if config.documents.enabled:
        from ragpilot.documents.pipeline import document_processor

        registry.register(FileKind.DOCUMENT, document_processor)
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
    ctx: AppContext, source: Source, processors: ProcessorRegistry
) -> SourcePassResult:
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
    result = coordinator.run()
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
    embedded = 0
    if ctx.config.search.semantic and (
        result.touched_code_file_ids or result.touched_document_file_ids
    ):
        touched_file_ids = [*result.touched_code_file_ids, *result.touched_document_file_ids]
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
                touched_code_file_ids=result.touched_code_file_ids,
                touched_document_file_ids=result.touched_document_file_ids,
            )
        if embedded:
            # Deliberately outside the transaction above: the ANN index
            # is a separate on-disk file, not part of the SQLite
            # transaction's atomicity guarantee -- SQLite (already
            # committed at this point) remains the authoritative source
            # it can always be rebuilt from (blueprint section 49), so a
            # failure here degrades to "ANN index lags until the next
            # sync or an explicit `ragpilot vectors rebuild`", never to
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
