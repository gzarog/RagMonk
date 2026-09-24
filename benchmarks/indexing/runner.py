"""Drives one benchmark scenario through the real indexing entry point
(``ragmonk.indexing.runner.run_source_pass`` -- the exact code path
``ragmonk index`` and the daemon both use) against a generated fixture
corpus, and records the metrics defined in ``metrics.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing import runner as index_runner
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import entities_repo

from benchmarks.indexing.metrics import (
    ScenarioMetrics,
    count_hash_calls,
    environment_info,
    measure_resources,
)

try:
    from ragmonk.storage.repositories import documents_repo

    _HAS_DOCUMENTS_REPO = True
except ImportError:  # pragma: no cover - documents_repo always ships today
    _HAS_DOCUMENTS_REPO = False


@dataclass
class CorrectnessSnapshot:
    entities: int
    document_sections: int


def _snapshot_correctness(ctx: AppContext, source_path: Path) -> CorrectnessSnapshot:
    project_id = paths.project_id_for_path(source_path)
    conn = ctx.project_conn(project_id)
    entities = entities_repo.count_all(conn)
    sections = documents_repo.count_all(conn) if _HAS_DOCUMENTS_REPO else 0
    return CorrectnessSnapshot(entities=entities, document_sections=sections)


def run_scenario(
    *,
    home: Path,
    source_path: Path,
    scenario: str,
    corpus_tier: str,
    config: RagMonkConfig | None = None,
) -> tuple[ScenarioMetrics, CorrectnessSnapshot]:
    """Runs exactly one indexing pass over ``source_path`` (adding it as
    a source on first use; reusing the same on-disk home/project DB on
    later calls so "warm", "single edit", etc. scenarios measure
    incremental behavior against real prior state) and returns the
    measured metrics plus a correctness snapshot.
    """
    ctx = AppContext.bootstrap(home=home, cwd=source_path, cli_overrides=None)
    try:
        if config is not None:
            ctx.config = config
        registry = SourceRegistry(ctx.sources_conn, home=home)
        source = registry.add(str(source_path))
        processors = index_runner.build_processor_registry(ctx.config)

        with measure_resources() as usage, count_hash_calls() as hash_counters:
            pass_result = index_runner.run_source_pass(ctx, source, processors)

        result = pass_result.result
        metrics = ScenarioMetrics(
            scenario=scenario,
            corpus_tier=corpus_tier,
            wall_time_s=usage["wall_time_s"],
            scanned=result.scanned,
            new=result.new,
            changed=result.changed,
            unchanged=result.unchanged,
            deleted=result.deleted,
            moved=result.moved,
            indexed=result.indexed,
            skipped_limit=result.skipped_limit,
            failed=result.failed,
            hash_calls=hash_counters.calls,
            hash_seconds=hash_counters.total_seconds,
            hash_bytes=hash_counters.bytes_hashed,
            cpu_user_s=usage["cpu_user_s"],
            cpu_sys_s=usage["cpu_sys_s"],
            peak_rss_mb=usage["peak_rss_mb"],
        )
        correctness = _snapshot_correctness(ctx, source_path)
        return metrics, correctness
    finally:
        ctx.close()


__all__ = ["run_scenario", "CorrectnessSnapshot", "environment_info"]
