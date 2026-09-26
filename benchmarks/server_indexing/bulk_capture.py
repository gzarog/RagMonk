"""Instrumentation that observes the REAL bulk-publish path
(``ragmonk.backends.opensearch_bulk``/``elasticsearch_bulk``) without
changing its behavior at all -- every action still goes through the
genuine ``run_bulk``/``_send_batch``/``_send_batch_with_retries``
functions; this module only wraps them to count what already happens,
for the benchmark's machine-readable report.

Both bulk modules are structurally identical (independently implemented
per-engine, per their own docstrings), so one capture class drives
either by module reference.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BulkStats:
    bulk_actions: int = 0
    bulk_requests: int = 0  # every HTTP bulk call, including retry attempts
    initial_batches: int = 0  # batches formed before any retry
    batch_sizes: list[int] = field(default_factory=list)  # sizes of initial batches
    retries: int = 0  # bulk_requests beyond one-per-initial-batch
    retryable_failures_seen: int = 0  # retryable-status failures across all attempts
    terminal_failures: int = 0  # failures still standing after retries exhausted

    @property
    def avg_batch_size(self) -> float:
        return sum(self.batch_sizes) / len(self.batch_sizes) if self.batch_sizes else 0.0

    @property
    def max_batch_size(self) -> int:
        return max(self.batch_sizes) if self.batch_sizes else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bulk_actions": self.bulk_actions,
            "bulk_requests": self.bulk_requests,
            "batches": self.initial_batches,
            "avg_batch_size": round(self.avg_batch_size, 2),
            "max_batch_size": self.max_batch_size,
            "retries": self.retries,
            "retryable_failures_seen": self.retryable_failures_seen,
            "terminal_failures": self.terminal_failures,
        }


@contextmanager
def capture_bulk_stats(bulk_module: types.ModuleType) -> Iterator[BulkStats]:
    """Patches ``bulk_module`` (``opensearch_bulk`` or
    ``elasticsearch_bulk``) in place for the duration of the ``with``
    block, returning a live :class:`BulkStats` that fills in as real
    bulk calls happen. Restores the original functions on exit
    unconditionally.
    """
    stats = BulkStats()
    original_batch: Any = bulk_module._batch
    original_send_batch: Any = bulk_module._send_batch
    original_run_bulk: Any = bulk_module.run_bulk

    def _counting_batch(actions: Any, max_actions: int, max_bytes: int) -> Any:
        batches = list(original_batch(actions, max_actions, max_bytes))
        stats.initial_batches += len(batches)
        stats.batch_sizes.extend(len(b) for b in batches)
        return batches

    def _counting_send_batch(client: Any, batch: Any) -> Any:
        stats.bulk_requests += 1
        succeeded, failures = original_send_batch(client, batch)
        retryable = {409, 429, 500, 502, 503, 504}
        stats.retryable_failures_seen += sum(1 for f in failures if f.status in retryable)
        return succeeded, failures

    def _counting_run_bulk(client: Any, actions: Any, config: Any) -> Any:
        result = original_run_bulk(client, actions, config)
        stats.terminal_failures += len(result.failed)
        return result

    bulk_module._batch = _counting_batch  # type: ignore[attr-defined]
    bulk_module._send_batch = _counting_send_batch  # type: ignore[attr-defined]
    bulk_module.run_bulk = _counting_run_bulk  # type: ignore[attr-defined]
    try:
        yield stats
    finally:
        bulk_module._batch = original_batch  # type: ignore[attr-defined]
        bulk_module._send_batch = original_send_batch  # type: ignore[attr-defined]
        bulk_module.run_bulk = original_run_bulk  # type: ignore[attr-defined]
        stats.bulk_actions = sum(stats.batch_sizes)
        stats.retries = max(0, stats.bulk_requests - stats.initial_batches)


@contextmanager
def capture_bulk_stats_both() -> Iterator[BulkStats]:
    """Captures stats across BOTH ``opensearch_bulk`` and
    ``elasticsearch_bulk`` simultaneously, merging into one
    :class:`BulkStats`, so the caller doesn't need to know in advance
    which engine's adapter a given ``KnowledgeBackend`` resolves to.
    """
    from ragmonk.backends import elasticsearch_bulk, opensearch_bulk

    merged = BulkStats()
    with capture_bulk_stats(opensearch_bulk) as os_stats, capture_bulk_stats(
        elasticsearch_bulk
    ) as es_stats:
        yield merged
    for stats in (os_stats, es_stats):
        merged.bulk_actions += stats.bulk_actions
        merged.bulk_requests += stats.bulk_requests
        merged.initial_batches += stats.initial_batches
        merged.batch_sizes.extend(stats.batch_sizes)
        merged.retries += stats.retries
        merged.retryable_failures_seen += stats.retryable_failures_seen
        merged.terminal_failures += stats.terminal_failures


__all__ = ["BulkStats", "capture_bulk_stats", "capture_bulk_stats_both"]
