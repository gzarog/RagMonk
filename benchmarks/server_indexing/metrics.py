"""Metric dataclasses and percentile helpers for the real-server
indexing benchmark. Pure data + arithmetic -- no backend imports here,
so this module is trivially unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (``pct`` in [0, 100]). Returns 0.0 for an
    empty input rather than raising -- a benchmark run that issued zero
    queries should report a zero latency, not crash the report.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[rank]


@dataclass
class QueryLatencyStats:
    label: str
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_samples(cls, label: str, samples_ms: list[float]) -> QueryLatencyStats:
        return cls(
            label=label,
            samples=len(samples_ms),
            p50_ms=round(percentile(samples_ms, 50), 3),
            p95_ms=round(percentile(samples_ms, 95), 3),
            max_ms=round(max(samples_ms), 3) if samples_ms else 0.0,
        )


@dataclass
class IndexRunMetrics:
    """One indexing pass's measured cost, published against a real
    server ``KnowledgeBackend``.
    """

    scenario: str
    files_scanned: int = 0
    files_new: int = 0
    files_changed: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    files_moved: int = 0
    files_indexed: int = 0
    files_failed: int = 0

    wall_time_s: float = 0.0
    scan_seconds: float = 0.0
    classify_seconds: float = 0.0
    process_seconds: float = 0.0  # parse/prepare + server publish, combined (see docs)
    linking_seconds: float = 0.0
    embedding_seconds: float = 0.0
    ann_sync_seconds: float = 0.0

    files_per_sec: float = 0.0
    bytes_per_sec: float = 0.0
    total_bytes: int = 0

    bulk_actions: int = 0
    bulk_requests: int = 0
    bulk_batches: int = 0
    avg_batch_size: float = 0.0
    max_batch_size: int = 0
    bulk_retries: int = 0
    retryable_failures_seen: int = 0
    terminal_failures: int = 0

    entity_docs_before: int = 0
    entity_docs_after: int = 0
    file_docs_before: int = 0
    file_docs_after: int = 0

    def finalize(self) -> None:
        if self.wall_time_s:
            self.files_per_sec = round(self.files_scanned / self.wall_time_s, 3)
            self.bytes_per_sec = round(self.total_bytes / self.wall_time_s, 3)
        else:
            self.files_per_sec = 0.0
            self.bytes_per_sec = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncrementalRunMetrics:
    """The second-pass, incremental-update measurement: modifies/adds/
    deletes/renames a slice of a previously-indexed corpus and reruns
    the real indexing entry point, so the report makes it obvious
    whether the incremental path only touched what changed (small
    ``files_scanned``/``files_reprocessed`` relative to corpus size,
    with the *right* documents added/removed server-side) or incorrectly
    rewrote the entire corpus.
    """

    corpus_size: int
    files_modified_on_disk: int
    files_added_on_disk: int
    files_deleted_on_disk: int
    files_renamed_on_disk: int

    index_metrics: IndexRunMetrics

    entity_docs_deleted: int = 0
    file_docs_deleted: int = 0

    @property
    def looks_like_full_rewrite(self) -> bool:
        """True when the pass reprocessed a share of the corpus far
        larger than the actual on-disk change -- the tell-tale sign of
        an incremental path silently degrading into a full reindex.
        """
        changed_on_disk = (
            self.files_modified_on_disk
            + self.files_added_on_disk
            + self.files_deleted_on_disk
            + self.files_renamed_on_disk
        )
        if self.corpus_size == 0:
            return False
        reprocessed = self.index_metrics.files_indexed
        # Generous slack (3x the real change, floor of 25 files) before
        # calling it a full-rewrite regression -- linking/rename
        # reconciliation can legitimately touch a few extra files.
        threshold = max(25, changed_on_disk * 3)
        return reprocessed > threshold and reprocessed > self.corpus_size * 0.5

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["looks_like_full_rewrite"] = self.looks_like_full_rewrite
        return data


@dataclass
class BenchmarkReport:
    label: str
    engine: str
    real_cluster: bool
    harness_smoke_test: bool
    environment: dict[str, Any] = field(default_factory=dict)
    cold_index: dict[str, Any] | None = None
    incremental: dict[str, Any] | None = None
    lexical_latency: dict[str, Any] | None = None
    hybrid_latency: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "percentile",
    "QueryLatencyStats",
    "IndexRunMetrics",
    "IncrementalRunMetrics",
    "BenchmarkReport",
]
