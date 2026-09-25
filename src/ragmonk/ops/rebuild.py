"""``ragmonk rebuild``: wipe one or every source's derived ``knowledge.db``
and re-index it from scratch.

The blueprint's disaster-recovery principle made concrete: "source files
= truth, RagMonk DB = rebuildable derived state." If a project's
``knowledge.db`` is damaged, or a clean rebuild is just wanted, this
deletes that database (source files on disk are never touched) and runs
the exact same per-source indexing pass ``ragmonk index`` and the Phase
7 daemon already use (``indexing/runner.py``'s ``run_source_pass``) --
this module is deliberately thin, a "delete then reuse the existing
pipeline" wrapper rather than a second indexer.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path

from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Source
from ragmonk.indexing.coordinator import IndexRunResult, ProcessorRegistry
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry


@dataclass(frozen=True)
class RebuildOutcome:
    source: Source
    result: IndexRunResult
    linked: int


def _project_derived_paths(ctx: AppContext, project_id: str) -> list[Path]:
    """Every on-disk file that makes up a project's derived state: the
    ``knowledge.db`` (+ its WAL/SHM sidecars) and the vector index and its
    metadata. Source files are never included -- they are the truth this
    derived state is rebuilt *from*.
    """
    db_path = paths.project_db_path(project_id, ctx.home)
    paths_list = [db_path.with_name(db_path.name + suffix) for suffix in ("", "-wal", "-shm")]
    paths_list.append(paths.project_vector_index_path(project_id, ctx.home))
    paths_list.append(paths.project_vector_meta_path(project_id, ctx.home))
    return paths_list


def _wipe_project_db(ctx: AppContext, project_id: str) -> None:
    ctx.close_project_conn(project_id)
    db_path = paths.project_db_path(project_id, ctx.home)
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _backup_derived_state(ctx: AppContext, project_id: str) -> list[tuple[Path, Path]]:
    """Moves a project's derived files aside to ``*.old`` backups so the
    previous index survives until the fresh rebuild activates
    successfully. Returns the (original, backup) pairs to restore/discard.
    """
    ctx.close_project_conn(project_id)
    moved: list[tuple[Path, Path]] = []
    for original in _project_derived_paths(ctx, project_id):
        if original.exists():
            backup = original.with_name(original.name + ".old")
            backup.unlink(missing_ok=True)
            original.replace(backup)
            moved.append((original, backup))
    return moved


def _discard_backup(moved: list[tuple[Path, Path]]) -> None:
    for _original, backup in moved:
        backup.unlink(missing_ok=True)


def _restore_backup(ctx: AppContext, project_id: str, moved: list[tuple[Path, Path]]) -> None:
    """Rolls a failed fresh rebuild back to the previously active index:
    removes whatever partial derived state was written, then moves the
    backups back into place.
    """
    ctx.close_project_conn(project_id)
    for partial in _project_derived_paths(ctx, project_id):
        partial.unlink(missing_ok=True)
    for original, backup in moved:
        if backup.exists():
            backup.replace(original)


def _generation_as_int(generation: str) -> int:
    """Coerces a backend-issued generation id to the ``int`` shape
    ``PreparedCode.generation``/``PreparedDocument.generation`` (and
    ``IndexCoordinator``'s ``force_generation``) require.

    ``OpenSearchKnowledgeBackend.begin_generation``/
    ``ElasticsearchKnowledgeBackend.begin_generation`` normally return a
    plain incrementing decimal string ("1", "2", ...), which round-trips
    through ``int()`` exactly. Their own fallback (a ``uuid4().hex`` id,
    only reached if the stored marker's ``active_generation`` field is
    somehow not int-parseable) is not decimal, so this falls back to a
    stable, deterministic non-negative hash of the string instead of
    raising -- any two calls with the same generation string must
    produce the same int, since this value is later compared for
    equality against what ``abort_generation``'s delete-by-query targets.
    """
    try:
        return int(generation)
    except ValueError:
        return zlib.crc32(generation.encode("utf-8"))


def _rebuild_source_server(
    ctx: AppContext, source: Source, processors: ProcessorRegistry
) -> RebuildOutcome:
    """Server-mode full rebuild for one source (Storage backend
    abstraction plan, Phase 7): wraps the whole re-index pass in a new
    write generation obtained from ``ctx.backend()`` so a reader never
    observes a half-rebuilt source.

    - ``begin_generation`` opens a new generation before anything is
      touched.
    - The local per-project ``knowledge.db`` scan-state (the file/job
      tables ``IndexCoordinator`` diffs against) is wiped first, exactly
      like the non-``--fresh`` local path below, so every file classifies
      as NEW and is fully reprocessed rather than skipped as UNCHANGED --
      a full rebuild must republish every file's content, not just
      touched ones.
    - The pass runs through ``ctx.backend()`` (the cached server
      adapter), with every file's generation pinned to the new
      generation's int form, so every entity/relationship/document/chunk
      document this pass writes is tagged with exactly the generation
      ``abort_generation`` would need to delete.
    - On full success, ``publish_generation`` atomically makes the new
      generation the active one.
    - On ANY exception (a raised, non-per-file failure -- a per-file
      processing error is recorded in ``result.failed``/``errors_repo``
      and does not raise, matching this same distinction the existing
      local ``--fresh`` path already draws), ``abort_generation`` deletes
      this generation's incomplete documents and re-raises; the
      previously published generation's documents (any file this pass
      never reached) are never touched, so the source stays searchable
      on the old generation throughout.
    """
    backend = ctx.backend()
    generation = backend.begin_generation(source.id)
    project_id = paths.project_id_for_path(Path(source.path))
    _wipe_project_db(ctx, project_id)
    try:
        pass_result = run_source_pass(
            ctx,
            source,
            processors,
            backend=backend,
            force_generation=_generation_as_int(generation),
        )
    except BaseException:
        backend.abort_generation(source.id, generation)
        raise
    backend.publish_generation(source.id, generation)
    return RebuildOutcome(source=source, result=pass_result.result, linked=pass_result.linked)


def rebuild(
    ctx: AppContext, *, source_id: str | None = None, fresh: bool = False
) -> list[RebuildOutcome]:
    """Wipes and re-indexes one or every source's derived state from its
    registered source files.

    ``fresh`` (Exact Tokenizer plan, Phase 4) makes the rebuild recoverable:
    each project's existing derived state (``knowledge.db`` + vector index)
    is moved aside to ``*.old`` backups, the fresh index is built in its
    place, and the backups are only discarded once the rebuild completes
    without raising. A rebuild that raises mid-way is rolled back to the
    previously active index, so a failed ``rebuild --fresh`` never leaves a
    source with no usable index. Registered source roots are verified
    reachable before anything is touched.

    In server mode (``ctx.config.storage.mode == "server"``), rebuild-
    safety instead goes through the backend's generation lifecycle (see
    ``_rebuild_source_server``) rather than this ``*.old`` file-backup
    scheme, which only makes sense for the local per-project SQLite
    files -- ``fresh`` is accepted-and-ignored in that mode since a
    server-mode rebuild is *always* recoverable via its generation
    marker, unlike local mode where that safety is opt-in.
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    if source_id is not None:
        sources = [registry.get(source_id)]
    else:
        sources = registry.list(enabled_only=True)

    if not sources:
        raise UsageError("no sources to rebuild")

    if fresh and ctx.config.storage.mode != "server":
        unreachable = [s for s in sources if not Path(s.path).exists()]
        if unreachable:
            listed = ", ".join(f"{s.id} ({s.path})" for s in unreachable)
            raise UsageError(
                f"cannot rebuild --fresh: source root(s) not reachable: {listed}. "
                "Reconnect them (or remove the sources) and try again; the existing "
                "index was left untouched."
            )

    processors = build_processor_registry(ctx.config)

    if ctx.config.storage.mode == "server":
        return [_rebuild_source_server(ctx, source, processors) for source in sources]

    outcomes: list[RebuildOutcome] = []
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        if fresh:
            moved = _backup_derived_state(ctx, project_id)
            try:
                pass_result = run_source_pass(ctx, source, processors)
            except BaseException:
                _restore_backup(ctx, project_id, moved)
                raise
            _discard_backup(moved)
        else:
            _wipe_project_db(ctx, project_id)
            pass_result = run_source_pass(ctx, source, processors)
        outcomes.append(
            RebuildOutcome(source=source, result=pass_result.result, linked=pass_result.linked)
        )
    return outcomes
