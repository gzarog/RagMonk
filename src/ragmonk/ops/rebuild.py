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

from dataclasses import dataclass
from pathlib import Path

from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Source
from ragmonk.indexing.coordinator import IndexRunResult
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry


@dataclass(frozen=True)
class RebuildOutcome:
    source: Source
    result: IndexRunResult
    linked: int


def _wipe_project_db(ctx: AppContext, project_id: str) -> None:
    ctx.close_project_conn(project_id)
    db_path = paths.project_db_path(project_id, ctx.home)
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def rebuild(ctx: AppContext, *, source_id: str | None = None) -> list[RebuildOutcome]:
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    if source_id is not None:
        sources = [registry.get(source_id)]
    else:
        sources = registry.list(enabled_only=True)

    if not sources:
        raise UsageError("no sources to rebuild")

    processors = build_processor_registry(ctx.config)
    outcomes: list[RebuildOutcome] = []
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        _wipe_project_db(ctx, project_id)
        pass_result = run_source_pass(ctx, source, processors)
        outcomes.append(
            RebuildOutcome(source=source, result=pass_result.result, linked=pass_result.linked)
        )
    return outcomes
