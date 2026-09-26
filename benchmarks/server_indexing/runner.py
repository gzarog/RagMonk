"""Drives the REAL indexing pipeline (``ragmonk.indexing.runner.
run_source_pass`` -- the exact code path ``ragmonk index``/the daemon
use) against a generated corpus, publishing to a REAL server
``KnowledgeBackend`` (OpenSearch or Elasticsearch), and measures it.

This intentionally reuses the production ``KnowledgeBackend``/bulk
pipeline rather than building a second, parallel indexing path -- see
``benchmarks/indexing/runner.py`` for the equivalent local-backend
pattern this mirrors.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from benchmarks.server_indexing.bulk_capture import capture_bulk_stats_both
from benchmarks.server_indexing.metrics import (
    IncrementalRunMetrics,
    IndexRunMetrics,
    QueryLatencyStats,
)

from ragmonk.core.config import (
    BulkConfig,
    RagMonkConfig,
    ServerStorageConfig,
    StorageConfig,
)
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing import runner as index_runner
from ragmonk.sources.registry import SourceRegistry


def build_server_config(
    engine: str,
    url: str,
    *,
    index_prefix: str | None = None,
    verify_tls: bool = False,
    semantic: bool = False,
    max_actions: int = 500,
    max_bytes: int = 5_000_000,
    concurrency: int = 2,
    max_retries: int = 3,
) -> RagMonkConfig:
    config = RagMonkConfig(
        storage=StorageConfig(
            mode="server",
            server=ServerStorageConfig(
                engine=engine,  # type: ignore[arg-type]
                url=url,
                index_prefix=index_prefix or f"bench-{uuid.uuid4().hex[:8]}",
                verify_tls=verify_tls,
                bulk=BulkConfig(
                    max_actions=max_actions,
                    max_bytes=max_bytes,
                    concurrency=concurrency,
                    max_retries=max_retries,
                ),
            ),
        )
    )
    config.search.semantic = semantic
    return config


def _dir_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _doc_counts(backend: Any) -> tuple[int, int]:
    """Returns (file_docs, content_docs) currently in the server
    indices, via a direct ``count`` call -- best-effort: any error
    (e.g. index not yet created before the first pass) is treated as 0.
    """
    try:
        files_index, content_index, _rel_index = backend._index_names()
        client = backend._get_client()
        match_all: dict[str, Any] = {"query": {"match_all": {}}}
        files_count = int(client.count(index=files_index, body=match_all).get("count", 0))
        content_count = int(client.count(index=content_index, body=match_all).get("count", 0))
        return files_count, content_count
    except Exception:
        return 0, 0


def run_indexing_pass(
    *,
    home: Path,
    source_path: Path,
    config: RagMonkConfig,
    scenario: str,
    registry: SourceRegistry | None = None,
    ctx: AppContext | None = None,
) -> tuple[IndexRunMetrics, AppContext, SourceRegistry]:
    """Runs exactly one real indexing pass over ``source_path`` and
    returns the measured metrics plus the live ``AppContext``/
    ``SourceRegistry`` (reused across cold + incremental calls so the
    incremental pass sees genuine prior state, exactly like a real
    ``ragmonk index`` rerun would).
    """
    if ctx is None:
        ctx = AppContext.bootstrap(home=home, cwd=source_path, cli_overrides=None)
        ctx.config = config
    if registry is None:
        registry = SourceRegistry(ctx.sources_conn, home=home)
        source = registry.add(str(source_path))
    else:
        from ragmonk.storage.repositories import sources_repo

        record = sources_repo.get_by_path(ctx.sources_conn, str(source_path.resolve()))
        assert record is not None
        source = record

    processors = index_runner.build_processor_registry(ctx.config)
    backend = ctx.backend()

    before_files, before_content = _doc_counts(backend)
    total_bytes = _dir_bytes(source_path)

    with capture_bulk_stats_both() as bulk_stats:
        started = time.perf_counter()
        pass_result = index_runner.run_source_pass(ctx, source, processors)
        wall = time.perf_counter() - started

    after_files, after_content = _doc_counts(backend)

    result = pass_result.result
    metrics = IndexRunMetrics(
        scenario=scenario,
        files_scanned=result.scanned,
        files_new=result.new,
        files_changed=result.changed,
        files_unchanged=result.unchanged,
        files_deleted=result.deleted,
        files_moved=result.moved,
        files_indexed=result.indexed,
        files_failed=result.failed,
        wall_time_s=round(wall, 4),
        scan_seconds=round(result.timings.scan_seconds, 4),
        classify_seconds=round(result.timings.classify_seconds, 4),
        process_seconds=round(result.timings.process_seconds, 4),
        linking_seconds=round(result.timings.linking_seconds, 4),
        embedding_seconds=round(result.timings.embedding_seconds, 4),
        ann_sync_seconds=round(result.timings.ann_sync_seconds, 4),
        total_bytes=total_bytes,
        bulk_actions=bulk_stats.bulk_actions,
        bulk_requests=bulk_stats.bulk_requests,
        bulk_batches=bulk_stats.initial_batches,
        avg_batch_size=round(bulk_stats.avg_batch_size, 2),
        max_batch_size=bulk_stats.max_batch_size,
        bulk_retries=bulk_stats.retries,
        retryable_failures_seen=bulk_stats.retryable_failures_seen,
        terminal_failures=bulk_stats.terminal_failures,
        entity_docs_before=before_content,
        entity_docs_after=after_content,
        file_docs_before=before_files,
        file_docs_after=after_files,
    )
    metrics.finalize()
    return metrics, ctx, registry


def measure_query_latency(
    ctx: AppContext,
    queries: list[str],
    *,
    label: str,
    repeats: int = 3,
    limit: int = 20,
    hybrid: bool = False,
) -> QueryLatencyStats:
    from ragmonk.retrieval import lexical

    samples_ms: list[float] = []
    if hybrid:
        from ragmonk.retrieval import semantic

        for query in queries:
            for _ in range(repeats):
                started = time.perf_counter()
                semantic.semantic_search(
                    ctx,
                    query,
                    config=ctx.config.search,
                    limit=limit,
                    candidate_k=ctx.config.search.semantic_top_k,
                )
                samples_ms.append((time.perf_counter() - started) * 1000)
    else:
        for query in queries:
            for _ in range(repeats):
                timed = lexical.search_with_timings(ctx, query, limit=limit)
                total_ms = sum(t.duration_ms for t in timed.timings) or 0.0
                samples_ms.append(total_ms)
    return QueryLatencyStats.from_samples(label, samples_ms)


def representative_queries(n_files: int) -> list[str]:
    """A fixed, small set of lexical queries this corpus is guaranteed
    to have matches for (function/class names the generator always
    emits), plus a couple of prose-ish queries against the generated
    Markdown docs.
    """
    sample_indices = [0, 1, max(0, n_files // 4), max(0, n_files // 2), max(0, n_files - 2)]
    queries = [f"process_{i}" for i in sample_indices]
    queries += [f"Widget{i}" for i in sample_indices[:3]]
    queries += ["helper_0", "orchestrate_1", "Generated module", "benchmark"]
    return queries


def build_incremental_metrics(
    *,
    corpus_size: int,
    changes: dict[str, list[str]],
    index_metrics: IndexRunMetrics,
) -> IncrementalRunMetrics:
    entity_docs_deleted = 0
    if index_metrics.entity_docs_after < index_metrics.entity_docs_before:
        entity_docs_deleted = index_metrics.entity_docs_before - index_metrics.entity_docs_after
    file_docs_deleted = 0
    if index_metrics.file_docs_after < index_metrics.file_docs_before:
        file_docs_deleted = index_metrics.file_docs_before - index_metrics.file_docs_after
    return IncrementalRunMetrics(
        corpus_size=corpus_size,
        files_modified_on_disk=len(changes["modified"]),
        files_added_on_disk=len(changes["added"]),
        files_deleted_on_disk=len(changes["deleted"]),
        files_renamed_on_disk=len(changes["renamed"]),
        index_metrics=index_metrics,
        entity_docs_deleted=entity_docs_deleted,
        file_docs_deleted=file_docs_deleted,
    )


__all__ = [
    "build_server_config",
    "run_indexing_pass",
    "measure_query_latency",
    "representative_queries",
    "build_incremental_metrics",
]
